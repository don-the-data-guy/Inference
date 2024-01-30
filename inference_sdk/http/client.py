import itertools
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

import aiohttp
import numpy as np
import requests
from requests import HTTPError

from inference_sdk.http.entities import (
    CLASSIFICATION_TASK,
    INSTANCE_SEGMENTATION_TASK,
    KEYPOINTS_DETECTION_TASK,
    OBJECT_DETECTION_TASK,
    HTTPClientMode,
    ImagesReference,
    InferenceConfiguration,
    ModelDescription,
    RegisteredModels,
    ServerInfo,
)
from inference_sdk.http.errors import (
    HTTPCallErrorError,
    HTTPClientError,
    InvalidModelIdentifier,
    InvalidParameterError,
    ModelNotInitializedError,
    ModelNotSelectedError,
    ModelTaskTypeNotSupportedError,
    WrongClientModeError,
)
from inference_sdk.http.utils.executors import (
    RequestMethod,
    execute_requests_packages,
    execute_requests_packages_async,
)
from inference_sdk.http.utils.iterables import unwrap_single_element_list
from inference_sdk.http.utils.loaders import (
    load_static_inference_input,
    load_static_inference_input_async,
    load_stream_inference_input,
)
from inference_sdk.http.utils.post_processing import (
    adjust_prediction_to_client_scaling_factor,
    combine_clip_embeddings,
    combine_gaze_detections,
    decode_workflow_outputs,
    response_contains_jpeg_image,
    transform_base64_visualisation,
    transform_visualisation_bytes,
)
from inference_sdk.http.utils.request_building import (
    ImagePlacement,
    prepare_requests_data,
)
from inference_sdk.http.utils.requests import (
    api_key_safe_raise_for_status,
    inject_images_into_payload,
)

SUCCESSFUL_STATUS_CODE = 200
DEFAULT_HEADERS = {
    "Content-Type": "application/json",
}
NEW_INFERENCE_ENDPOINTS = {
    INSTANCE_SEGMENTATION_TASK: "/infer/instance_segmentation",
    OBJECT_DETECTION_TASK: "/infer/object_detection",
    CLASSIFICATION_TASK: "/infer/classification",
    KEYPOINTS_DETECTION_TASK: "/infer/keypoints_detection",
}
CLIP_ARGUMENT_TYPES = {"image", "text"}


def wrap_errors(function: callable) -> callable:
    def decorate(*args, **kwargs) -> Any:
        try:
            return function(*args, **kwargs)
        except HTTPError as error:
            if "application/json" in error.response.headers.get("Content-Type", ""):
                api_message = error.response.json().get("message")
            else:
                api_message = error.response.text
            raise HTTPCallErrorError(
                description=str(error),
                status_code=error.response.status_code,
                api_message=api_message,
            ) from error
        except ConnectionError as error:
            raise HTTPClientError(
                f"Error with server connection: {str(error)}"
            ) from error

    return decorate


class InferenceHTTPClient:
    def __init__(
        self,
        api_url: str,
        api_key: str,
    ):
        self.__api_url = api_url
        self.__api_key = api_key
        self.__inference_configuration = InferenceConfiguration.init_default()
        self.__client_mode = _determine_client_mode(api_url=api_url)
        self.__selected_model: Optional[str] = None

    @property
    def inference_configuration(self) -> InferenceConfiguration:
        return self.__inference_configuration

    @property
    def client_mode(self) -> HTTPClientMode:
        return self.__client_mode

    @property
    def selected_model(self) -> Optional[str]:
        return self.__selected_model

    @contextmanager
    def use_configuration(
        self, inference_configuration: InferenceConfiguration
    ) -> Generator["InferenceHTTPClient", None, None]:
        previous_configuration = self.__inference_configuration
        self.__inference_configuration = inference_configuration
        try:
            yield self
        finally:
            self.__inference_configuration = previous_configuration

    def configure(
        self, inference_configuration: InferenceConfiguration
    ) -> "InferenceHTTPClient":
        self.__inference_configuration = inference_configuration
        return self

    def select_api_v0(self) -> "InferenceHTTPClient":
        self.__client_mode = HTTPClientMode.V0
        return self

    def select_api_v1(self) -> "InferenceHTTPClient":
        self.__client_mode = HTTPClientMode.V1
        return self

    @contextmanager
    def use_api_v0(self) -> Generator["InferenceHTTPClient", None, None]:
        previous_client_mode = self.__client_mode
        self.__client_mode = HTTPClientMode.V0
        try:
            yield self
        finally:
            self.__client_mode = previous_client_mode

    @contextmanager
    def use_api_v1(self) -> Generator["InferenceHTTPClient", None, None]:
        previous_client_mode = self.__client_mode
        self.__client_mode = HTTPClientMode.V1
        try:
            yield self
        finally:
            self.__client_mode = previous_client_mode

    def select_model(self, model_id: str) -> "InferenceHTTPClient":
        self.__selected_model = model_id
        return self

    @contextmanager
    def use_model(self, model_id: str) -> Generator["InferenceHTTPClient", None, None]:
        previous_model = self.__selected_model
        self.__selected_model = model_id
        try:
            yield self
        finally:
            self.__selected_model = previous_model

    @wrap_errors
    def get_server_info(self) -> ServerInfo:
        response = requests.get(f"{self.__api_url}/info")
        response.raise_for_status()
        response_payload = response.json()
        return ServerInfo.from_dict(response_payload)

    def infer_on_stream(
        self,
        input_uri: str,
        model_id: Optional[str] = None,
    ) -> Generator[Tuple[Union[str, int], np.ndarray, dict], None, None]:
        for reference, frame in load_stream_inference_input(
            input_uri=input_uri,
            image_extensions=self.__inference_configuration.image_extensions_for_directory_scan,
        ):
            prediction = self.infer(
                inference_input=frame,
                model_id=model_id,
            )
            yield reference, frame, prediction

    @wrap_errors
    def infer(
        self,
        inference_input: Union[ImagesReference, List[ImagesReference]],
        model_id: Optional[str] = None,
    ) -> Union[dict, List[dict]]:
        if self.__client_mode is HTTPClientMode.V0:
            return self.infer_from_api_v0(
                inference_input=inference_input,
                model_id=model_id,
            )
        return self.infer_from_api_v1(
            inference_input=inference_input,
            model_id=model_id,
        )

    @wrap_errors
    async def infer_async(
        self,
        inference_input: Union[ImagesReference, List[ImagesReference]],
        model_id: Optional[str] = None,
    ) -> Union[dict, List[dict]]:
        if self.__client_mode is HTTPClientMode.V0:
            return await self.infer_from_api_v0_async(
                inference_input=inference_input,
                model_id=model_id,
            )
        return await self.infer_from_api_v1_async(
            inference_input=inference_input,
            model_id=model_id,
        )

    def infer_from_api_v0(
        self,
        inference_input: Union[ImagesReference, List[ImagesReference]],
        model_id: Optional[str] = None,
    ) -> Union[dict, List[dict]]:
        model_id_to_be_used = model_id or self.__selected_model
        _ensure_model_is_selected(model_id=model_id_to_be_used)
        model_id_chunks = model_id_to_be_used.split("/")
        if len(model_id_chunks) != 2:
            raise InvalidModelIdentifier(
                f"Invalid model identifier: {model_id} in use."
            )
        max_height, max_width = _determine_client_downsizing_parameters(
            client_downsizing_disabled=self.__inference_configuration.client_downsizing_disabled,
            model_description=None,
            default_max_input_size=self.__inference_configuration.default_max_input_size,
        )
        encoded_inference_inputs = load_static_inference_input(
            inference_input=inference_input,
            max_height=max_height,
            max_width=max_width,
        )
        params = {
            "api_key": self.__api_key,
        }
        params.update(self.__inference_configuration.to_legacy_call_parameters())
        requests_data = prepare_requests_data(
            url=f"{self.__api_url}/{model_id_chunks[0]}/{model_id_chunks[1]}",
            encoded_inference_inputs=encoded_inference_inputs,
            headers=DEFAULT_HEADERS,
            parameters=params,
            payload=None,
            max_batch_size=1,
            image_placement=ImagePlacement.DATA,
        )
        responses = execute_requests_packages(
            requests_data=requests_data,
            request_method=RequestMethod.POST,
            max_concurrent_requests=self.__inference_configuration.max_concurent_requests,
        )
        results = []
        for request_data, response in zip(requests_data, responses):
            if response_contains_jpeg_image(response=response):
                visualisation = transform_visualisation_bytes(
                    visualisation=response.content,
                    expected_format=self.__inference_configuration.output_visualisation_format,
                )
                parsed_response = {"visualization": visualisation}
            else:
                parsed_response = response.json()
            parsed_response = adjust_prediction_to_client_scaling_factor(
                prediction=parsed_response,
                scaling_factor=request_data.image_scaling_factors[0],
            )
            results.append(parsed_response)
        return unwrap_single_element_list(sequence=results)

    async def infer_from_api_v0_async(
        self,
        inference_input: Union[ImagesReference, List[ImagesReference]],
        model_id: Optional[str] = None,
    ) -> Union[dict, List[dict]]:
        model_id_to_be_used = model_id or self.__selected_model
        _ensure_model_is_selected(model_id=model_id_to_be_used)
        model_id_chunks = model_id_to_be_used.split("/")
        if len(model_id_chunks) != 2:
            raise InvalidModelIdentifier(
                f"Invalid model identifier: {model_id} in use."
            )
        max_height, max_width = _determine_client_downsizing_parameters(
            client_downsizing_disabled=self.__inference_configuration.client_downsizing_disabled,
            model_description=None,
            default_max_input_size=self.__inference_configuration.default_max_input_size,
        )
        encoded_inference_inputs = await load_static_inference_input_async(
            inference_input=inference_input,
            max_height=max_height,
            max_width=max_width,
        )
        params = {
            "api_key": self.__api_key,
        }
        params.update(self.__inference_configuration.to_legacy_call_parameters())
        requests_data = prepare_requests_data(
            url=f"{self.__api_url}/{model_id_chunks[0]}/{model_id_chunks[1]}",
            encoded_inference_inputs=encoded_inference_inputs,
            headers=DEFAULT_HEADERS,
            parameters=params,
            payload=None,
            max_batch_size=1,
            image_placement=ImagePlacement.DATA,
        )
        responses = await execute_requests_packages_async(
            requests_data=requests_data,
            request_method=RequestMethod.POST,
            max_concurrent_requests=self.__inference_configuration.max_concurent_requests,
        )
        results = []
        for request_data, response in zip(requests_data, responses):
            if not issubclass(type(response), dict):
                visualisation = transform_visualisation_bytes(
                    visualisation=response,
                    expected_format=self.__inference_configuration.output_visualisation_format,
                )
                parsed_response = {"visualization": visualisation}
            else:
                parsed_response = response
            parsed_response = adjust_prediction_to_client_scaling_factor(
                prediction=parsed_response,
                scaling_factor=request_data.image_scaling_factors[0],
            )
            results.append(parsed_response)
        return unwrap_single_element_list(sequence=results)

    def infer_from_api_v1(
        self,
        inference_input: Union[ImagesReference, List[ImagesReference]],
        model_id: Optional[str] = None,
    ) -> Union[dict, List[dict]]:
        self.__ensure_v1_client_mode()
        model_id_to_be_used = model_id or self.__selected_model
        _ensure_model_is_selected(model_id=model_id_to_be_used)
        model_description = self.get_model_description(model_id=model_id_to_be_used)
        max_height, max_width = _determine_client_downsizing_parameters(
            client_downsizing_disabled=self.__inference_configuration.client_downsizing_disabled,
            model_description=model_description,
            default_max_input_size=self.__inference_configuration.default_max_input_size,
        )
        if model_description.task_type not in NEW_INFERENCE_ENDPOINTS:
            raise ModelTaskTypeNotSupportedError(
                f"Model task {model_description.task_type} is not supported by API v1 client."
            )
        encoded_inference_inputs = load_static_inference_input(
            inference_input=inference_input,
            max_height=max_height,
            max_width=max_width,
        )
        payload = {
            "api_key": self.__api_key,
            "model_id": model_id_to_be_used,
        }
        endpoint = NEW_INFERENCE_ENDPOINTS[model_description.task_type]
        payload.update(
            self.__inference_configuration.to_api_call_parameters(
                client_mode=self.__client_mode,
                task_type=model_description.task_type,
            )
        )
        requests_data = prepare_requests_data(
            url=f"{self.__api_url}{endpoint}",
            encoded_inference_inputs=encoded_inference_inputs,
            headers=DEFAULT_HEADERS,
            parameters=None,
            payload=payload,
            max_batch_size=self.__inference_configuration.max_batch_size,
            image_placement=ImagePlacement.JSON,
        )
        responses = execute_requests_packages(
            requests_data=requests_data,
            request_method=RequestMethod.POST,
            max_concurrent_requests=self.__inference_configuration.max_concurent_requests,
        )
        results = []
        for request_data, response in zip(requests_data, responses):
            parsed_response = response.json()
            if not issubclass(type(parsed_response), list):
                parsed_response = [parsed_response]
            for parsed_response_element, scaling_factor in zip(
                parsed_response, request_data.image_scaling_factors
            ):
                if parsed_response_element.get("visualization") is not None:
                    parsed_response_element["visualization"] = (
                        transform_base64_visualisation(
                            visualisation=parsed_response_element["visualization"],
                            expected_format=self.__inference_configuration.output_visualisation_format,
                        )
                    )
                parsed_response_element = adjust_prediction_to_client_scaling_factor(
                    prediction=parsed_response_element,
                    scaling_factor=scaling_factor,
                )
                results.append(parsed_response_element)
        return unwrap_single_element_list(sequence=results)

    async def infer_from_api_v1_async(
        self,
        inference_input: Union[ImagesReference, List[ImagesReference]],
        model_id: Optional[str] = None,
    ) -> Union[dict, List[dict]]:
        self.__ensure_v1_client_mode()
        model_id_to_be_used = model_id or self.__selected_model
        _ensure_model_is_selected(model_id=model_id_to_be_used)
        model_description = await self.get_model_description_async(
            model_id=model_id_to_be_used
        )
        max_height, max_width = _determine_client_downsizing_parameters(
            client_downsizing_disabled=self.__inference_configuration.client_downsizing_disabled,
            model_description=model_description,
            default_max_input_size=self.__inference_configuration.default_max_input_size,
        )
        if model_description.task_type not in NEW_INFERENCE_ENDPOINTS:
            raise ModelTaskTypeNotSupportedError(
                f"Model task {model_description.task_type} is not supported by API v1 client."
            )
        encoded_inference_inputs = await load_static_inference_input_async(
            inference_input=inference_input,
            max_height=max_height,
            max_width=max_width,
        )
        payload = {
            "api_key": self.__api_key,
            "model_id": model_id_to_be_used,
        }
        endpoint = NEW_INFERENCE_ENDPOINTS[model_description.task_type]
        payload.update(
            self.__inference_configuration.to_api_call_parameters(
                client_mode=self.__client_mode,
                task_type=model_description.task_type,
            )
        )
        requests_data = prepare_requests_data(
            url=f"{self.__api_url}{endpoint}",
            encoded_inference_inputs=encoded_inference_inputs,
            headers=DEFAULT_HEADERS,
            parameters=None,
            payload=payload,
            max_batch_size=self.__inference_configuration.max_batch_size,
            image_placement=ImagePlacement.JSON,
        )
        responses = await execute_requests_packages_async(
            requests_data=requests_data,
            request_method=RequestMethod.POST,
            max_concurrent_requests=self.__inference_configuration.max_concurent_requests,
        )
        results = []
        for request_data, parsed_response in zip(requests_data, responses):
            if not issubclass(type(parsed_response), list):
                parsed_response = [parsed_response]
            for parsed_response_element, scaling_factor in zip(
                parsed_response, request_data.image_scaling_factors
            ):
                if parsed_response_element.get("visualization") is not None:
                    parsed_response_element["visualization"] = (
                        transform_base64_visualisation(
                            visualisation=parsed_response_element["visualization"],
                            expected_format=self.__inference_configuration.output_visualisation_format,
                        )
                    )
                parsed_response_element = adjust_prediction_to_client_scaling_factor(
                    prediction=parsed_response_element,
                    scaling_factor=scaling_factor,
                )
                results.append(parsed_response_element)
        return unwrap_single_element_list(sequence=results)

    def get_model_description(
        self, model_id: str, allow_loading: bool = True
    ) -> ModelDescription:
        self.__ensure_v1_client_mode()
        registered_models = self.list_loaded_models()
        matching_models = [
            e for e in registered_models.models if e.model_id == model_id
        ]
        if len(matching_models) > 0:
            return matching_models[0]
        if allow_loading is True:
            self.load_model(model_id=model_id)
            return self.get_model_description(model_id=model_id, allow_loading=False)
        raise ModelNotInitializedError(
            f"Model {model_id} is not initialised and cannot retrieve its description."
        )

    async def get_model_description_async(
        self, model_id: str, allow_loading: bool = True
    ) -> ModelDescription:
        self.__ensure_v1_client_mode()
        registered_models = await self.list_loaded_models_async()
        matching_models = [
            e for e in registered_models.models if e.model_id == model_id
        ]
        if len(matching_models) > 0:
            return matching_models[0]
        if allow_loading is True:
            await self.load_model_async(model_id=model_id)
            return await self.get_model_description_async(
                model_id=model_id, allow_loading=False
            )
        raise ModelNotInitializedError(
            f"Model {model_id} is not initialised and cannot retrieve its description."
        )

    @wrap_errors
    def list_loaded_models(self) -> RegisteredModels:
        self.__ensure_v1_client_mode()
        response = requests.get(f"{self.__api_url}/model/registry")
        response.raise_for_status()
        response_payload = response.json()
        return RegisteredModels.from_dict(response_payload)

    @wrap_errors
    async def list_loaded_models_async(self) -> RegisteredModels:
        self.__ensure_v1_client_mode()
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.__api_url}/model/registry") as response:
                response.raise_for_status()
                response_payload = await response.json()
                return RegisteredModels.from_dict(response_payload)

    @wrap_errors
    def load_model(
        self, model_id: str, set_as_default: bool = False
    ) -> RegisteredModels:
        self.__ensure_v1_client_mode()
        response = requests.post(
            f"{self.__api_url}/model/add",
            json={
                "model_id": model_id,
                "api_key": self.__api_key,
            },
            headers=DEFAULT_HEADERS,
        )
        response.raise_for_status()
        response_payload = response.json()
        if set_as_default:
            self.__selected_model = model_id
        return RegisteredModels.from_dict(response_payload)

    @wrap_errors
    async def load_model_async(
        self, model_id: str, set_as_default: bool = False
    ) -> RegisteredModels:
        self.__ensure_v1_client_mode()
        payload = {
            "model_id": model_id,
            "api_key": self.__api_key,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.__api_url}/model/add",
                json=payload,
                headers=DEFAULT_HEADERS,
            ) as response:
                response.raise_for_status()
                response_payload = await response.json()
        if set_as_default:
            self.__selected_model = model_id
        return RegisteredModels.from_dict(response_payload)

    @wrap_errors
    def unload_model(self, model_id: str) -> RegisteredModels:
        self.__ensure_v1_client_mode()
        response = requests.post(
            f"{self.__api_url}/model/remove",
            json={
                "model_id": model_id,
            },
            headers=DEFAULT_HEADERS,
        )
        response.raise_for_status()
        response_payload = response.json()
        if model_id == self.__selected_model:
            self.__selected_model = None
        return RegisteredModels.from_dict(response_payload)

    @wrap_errors
    async def unload_model_async(self, model_id: str) -> RegisteredModels:
        self.__ensure_v1_client_mode()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.__api_url}/model/remove",
                json={
                    "model_id": model_id,
                },
                headers=DEFAULT_HEADERS,
            ) as response:
                response.raise_for_status()
                response_payload = await response.json()
        if model_id == self.__selected_model:
            self.__selected_model = None
        return RegisteredModels.from_dict(response_payload)

    @wrap_errors
    def unload_all_models(self) -> RegisteredModels:
        self.__ensure_v1_client_mode()
        response = requests.post(f"{self.__api_url}/model/clear")
        response.raise_for_status()
        response_payload = response.json()
        self.__selected_model = None
        return RegisteredModels.from_dict(response_payload)

    @wrap_errors
    async def unload_all_models_async(self) -> RegisteredModels:
        self.__ensure_v1_client_mode()
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.__api_url}/model/clear") as response:
                response.raise_for_status()
                response_payload = await response.json()
        self.__selected_model = None
        return RegisteredModels.from_dict(response_payload)

    @wrap_errors
    def prompt_cogvlm(
        self,
        visual_prompt: ImagesReference,
        text_prompt: str,
        chat_history: Optional[List[Tuple[str, str]]] = None,
    ) -> dict:
        self.__ensure_v1_client_mode()  # Lambda does not support CogVLM, so we require v1 mode of client
        encoded_image = load_static_inference_input(
            inference_input=visual_prompt,
        )
        payload = {
            "api_key": self.__api_key,
            "model_id": "cogvlm",
            "prompt": text_prompt,
        }
        payload = inject_images_into_payload(
            payload=payload,
            encoded_images=encoded_image,
        )
        if chat_history is not None:
            payload["history"] = chat_history
        response = requests.post(
            f"{self.__api_url}/llm/cogvlm",
            json=payload,
            headers=DEFAULT_HEADERS,
        )
        api_key_safe_raise_for_status(response=response)
        return response.json()

    @wrap_errors
    def ocr_image(
        self,
        inference_input: Union[ImagesReference, List[ImagesReference]],
    ) -> Union[dict, List[dict]]:
        encoded_inference_inputs = load_static_inference_input(
            inference_input=inference_input,
        )
        payload = self.__initialise_payload()
        url = self.__wrap_url_with_api_key(f"{self.__api_url}/doctr/ocr")
        requests_data = prepare_requests_data(
            url=url,
            encoded_inference_inputs=encoded_inference_inputs,
            headers=DEFAULT_HEADERS,
            parameters=None,
            payload=payload,
            max_batch_size=1,
            image_placement=ImagePlacement.JSON,
        )
        responses = execute_requests_packages(
            requests_data=requests_data,
            request_method=RequestMethod.POST,
            max_concurrent_requests=self.__inference_configuration.max_concurent_requests,
        )
        results = [r.json() for r in responses]
        return unwrap_single_element_list(sequence=results)

    @wrap_errors
    async def ocr_image_async(
        self,
        inference_input: Union[ImagesReference, List[ImagesReference]],
    ) -> Union[dict, List[dict]]:
        encoded_inference_inputs = await load_static_inference_input_async(
            inference_input=inference_input,
        )
        payload = self.__initialise_payload()
        url = self.__wrap_url_with_api_key(f"{self.__api_url}/doctr/ocr")
        requests_data = prepare_requests_data(
            url=url,
            encoded_inference_inputs=encoded_inference_inputs,
            headers=DEFAULT_HEADERS,
            parameters=None,
            payload=payload,
            max_batch_size=1,
            image_placement=ImagePlacement.JSON,
        )
        responses = await execute_requests_packages_async(
            requests_data=requests_data,
            request_method=RequestMethod.POST,
            max_concurrent_requests=self.__inference_configuration.max_concurent_requests,
        )
        return unwrap_single_element_list(sequence=responses)

    @wrap_errors
    def detect_gazes(
        self,
        inference_input: Union[ImagesReference, List[ImagesReference]],
    ) -> Union[dict, List[dict]]:
        self.__ensure_v1_client_mode()  # Lambda does not support Gaze, so we require v1 mode of client
        result = self._post_images(
            inference_input=inference_input, endpoint="/gaze/gaze_detection"
        )
        return combine_gaze_detections(detections=result)

    @wrap_errors
    async def detect_gazes_async(
        self,
        inference_input: Union[ImagesReference, List[ImagesReference]],
    ) -> Union[dict, List[dict]]:
        self.__ensure_v1_client_mode()  # Lambda does not support Gaze, so we require v1 mode of client
        result = await self._post_images_async(
            inference_input=inference_input, endpoint="/gaze/gaze_detection"
        )
        return combine_gaze_detections(detections=result)

    @wrap_errors
    def get_clip_image_embeddings(
        self,
        inference_input: Union[ImagesReference, List[ImagesReference]],
    ) -> Union[dict, List[dict]]:
        result = self._post_images(
            inference_input=inference_input,
            endpoint="/clip/embed_image",
        )
        result = combine_clip_embeddings(embeddings=result)
        return unwrap_single_element_list(result)

    @wrap_errors
    async def get_clip_image_embeddings_async(
        self,
        inference_input: Union[ImagesReference, List[ImagesReference]],
    ) -> Union[dict, List[dict]]:
        result = await self._post_images_async(
            inference_input=inference_input,
            endpoint="/clip/embed_image",
        )
        result = combine_clip_embeddings(embeddings=result)
        return unwrap_single_element_list(result)

    @wrap_errors
    def get_clip_text_embeddings(
        self, text: Union[str, List[str]]
    ) -> Union[dict, List[dict]]:
        payload = self.__initialise_payload()
        payload["text"] = text
        response = requests.post(
            self.__wrap_url_with_api_key(f"{self.__api_url}/clip/embed_text"),
            json=payload,
            headers=DEFAULT_HEADERS,
        )
        api_key_safe_raise_for_status(response=response)
        return unwrap_single_element_list(sequence=response.json())

    @wrap_errors
    async def get_clip_text_embeddings_async(
        self, text: Union[str, List[str]]
    ) -> Union[dict, List[dict]]:
        payload = self.__initialise_payload()
        payload["text"] = text
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.__wrap_url_with_api_key(f"{self.__api_url}/clip/embed_text"),
                json=payload,
                headers=DEFAULT_HEADERS,
            ) as response:
                response.raise_for_status()
                response_payload = await response.json()
        return unwrap_single_element_list(sequence=response_payload)

    @wrap_errors
    def clip_compare(
        self,
        subject: Union[str, ImagesReference],
        prompt: Union[str, List[str], ImagesReference, List[ImagesReference]],
        subject_type: str = "image",
        prompt_type: str = "text",
    ) -> Union[dict, List[dict]]:
        """
        Both `subject_type` and `prompt_type` must be either "image" or "text"
        """
        if (
            subject_type not in CLIP_ARGUMENT_TYPES
            or prompt_type not in CLIP_ARGUMENT_TYPES
        ):
            raise InvalidParameterError(
                f"Could not accept `subject_type` and `prompt_type` with values different than {CLIP_ARGUMENT_TYPES}"
            )
        payload = self.__initialise_payload()
        payload["subject_type"] = subject_type
        payload["prompt_type"] = prompt_type
        if subject_type == "image":
            encoded_image = load_static_inference_input(
                inference_input=subject,
            )
            payload = inject_images_into_payload(
                payload=payload, encoded_images=encoded_image, key="subject"
            )
        else:
            payload["subject"] = subject
        if prompt_type == "image":
            encoded_inference_inputs = load_static_inference_input(
                inference_input=prompt,
            )
            payload = inject_images_into_payload(
                payload=payload, encoded_images=encoded_inference_inputs, key="prompt"
            )
        else:
            payload["prompt"] = prompt
        response = requests.post(
            self.__wrap_url_with_api_key(f"{self.__api_url}/clip/compare"),
            json=payload,
            headers=DEFAULT_HEADERS,
        )
        api_key_safe_raise_for_status(response=response)
        return response.json()

    @wrap_errors
    async def clip_compare_async(
        self,
        subject: Union[str, ImagesReference],
        prompt: Union[str, List[str], ImagesReference, List[ImagesReference]],
        subject_type: str = "image",
        prompt_type: str = "text",
    ) -> Union[dict, List[dict]]:
        """
        Both `subject_type` and `prompt_type` must be either "image" or "text"
        """
        if (
            subject_type not in CLIP_ARGUMENT_TYPES
            or prompt_type not in CLIP_ARGUMENT_TYPES
        ):
            raise InvalidParameterError(
                f"Could not accept `subject_type` and `prompt_type` with values different than {CLIP_ARGUMENT_TYPES}"
            )
        payload = self.__initialise_payload()
        payload["subject_type"] = subject_type
        payload["prompt_type"] = prompt_type
        if subject_type == "image":
            encoded_image = await load_static_inference_input_async(
                inference_input=subject,
            )
            payload = inject_images_into_payload(
                payload=payload, encoded_images=encoded_image, key="subject"
            )
        else:
            payload["subject"] = subject
        if prompt_type == "image":
            encoded_inference_inputs = await load_static_inference_input_async(
                inference_input=prompt,
            )
            payload = inject_images_into_payload(
                payload=payload, encoded_images=encoded_inference_inputs, key="prompt"
            )
        else:
            payload["prompt"] = prompt

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.__wrap_url_with_api_key(f"{self.__api_url}/clip/compare"),
                json=payload,
                headers=DEFAULT_HEADERS,
            ) as response:
                response.raise_for_status()
                return await response.json()

    @wrap_errors
    def infer_from_workflow(
        self,
        workspace_name: Optional[str] = None,
        workflow_name: Optional[str] = None,
        workflow_specification: Optional[dict] = None,
        images: Optional[Dict[str, Any]] = None,
        parameters: Optional[Dict[str, Any]] = None,
        excluded_fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Triggers inference from workflow specification at the inference HTTP
        side. Either (`workspace_name` and `workflow_name`) or `workflow_specification` must be
        provided. In the first case - definition of workflow will be fetched
        from Roboflow API, in the latter - `workflow_specification` will be
        used. `images` and `parameters` will be merged into workflow inputs,
        the distinction is made to make sure the SDK can easily serialise
        images and prepare a proper payload. Supported images are numpy arrays,
        PIL.Image and base64 images, links to images and local paths.
        `excluded_fields` will be added to request to filter out results
        of workflow execution at the server side.
        """
        named_workflow_specified = (workspace_name is not None) and (workflow_name is not None)
        print("named_workflow_specified", named_workflow_specified, "workflow_specification", workflow_specification is not None)
        if not (named_workflow_specified != (workflow_specification is not None)):
            raise InvalidParameterError(
                "Parameters (`workspace_name`, `workflow_name`) can be used mutually exclusive with "
                "`workflow_specification`, but at least one must be set."
            )
        if images is None:
            images = {}
        if parameters is None:
            parameters = {}
        payload = {"api_key": self.__api_key}
        runtime_parameters = {}
        for image_name, image in images.items():
            loaded_image = load_static_inference_input(
                inference_input=image,
            )
            inject_images_into_payload(
                payload=runtime_parameters,
                encoded_images=loaded_image,
                key=image_name,
            )
        runtime_parameters.update(parameters)
        payload["runtime_parameters"] = runtime_parameters
        if excluded_fields is not None:
            payload["excluded_fields"] = excluded_fields
        if workflow_specification is not None:
            payload["specification"] = workflow_specification
        if workflow_specification is not None:
            url = f"{self.__api_url}/infer/workflows"
        else:
            url = f"{self.__api_url}/infer/workflows/{workspace_name}/{workflow_name}"
        response = requests.post(
            url,
            json=payload,
            headers=DEFAULT_HEADERS,
        )
        api_key_safe_raise_for_status(response=response)
        workflow_outputs = response.json()["outputs"]
        return decode_workflow_outputs(
            workflow_outputs=workflow_outputs,
            expected_format=self.__inference_configuration.output_visualisation_format,
        )

    def _post_images(
        self,
        inference_input: Union[ImagesReference, List[ImagesReference]],
        endpoint: str,
        model_id: Optional[str] = None,
    ) -> Union[dict, List[dict]]:
        encoded_inference_inputs = load_static_inference_input(
            inference_input=inference_input,
        )
        payload = self.__initialise_payload()
        if model_id is not None:
            payload["model_id"] = model_id
        url = self.__wrap_url_with_api_key(f"{self.__api_url}{endpoint}")
        requests_data = prepare_requests_data(
            url=url,
            encoded_inference_inputs=encoded_inference_inputs,
            headers=DEFAULT_HEADERS,
            parameters=None,
            payload=payload,
            max_batch_size=self.__inference_configuration.max_batch_size,
            image_placement=ImagePlacement.JSON,
        )
        responses = execute_requests_packages(
            requests_data=requests_data,
            request_method=RequestMethod.POST,
            max_concurrent_requests=self.__inference_configuration.max_concurent_requests,
        )
        results = [r.json() for r in responses]
        return unwrap_single_element_list(sequence=results)

    async def _post_images_async(
        self,
        inference_input: Union[ImagesReference, List[ImagesReference]],
        endpoint: str,
        model_id: Optional[str] = None,
    ) -> Union[dict, List[dict]]:
        encoded_inference_inputs = await load_static_inference_input_async(
            inference_input=inference_input,
        )
        payload = self.__initialise_payload()
        if model_id is not None:
            payload["model_id"] = model_id
        url = self.__wrap_url_with_api_key(f"{self.__api_url}{endpoint}")
        requests_data = prepare_requests_data(
            url=url,
            encoded_inference_inputs=encoded_inference_inputs,
            headers=DEFAULT_HEADERS,
            parameters=None,
            payload=payload,
            max_batch_size=self.__inference_configuration.max_batch_size,
            image_placement=ImagePlacement.JSON,
        )
        responses = await execute_requests_packages_async(
            requests_data=requests_data,
            request_method=RequestMethod.POST,
            max_concurrent_requests=self.__inference_configuration.max_concurent_requests,
        )
        return unwrap_single_element_list(sequence=responses)

    def __initialise_payload(self) -> dict:
        if self.__client_mode is not HTTPClientMode.V0:
            return {"api_key": self.__api_key}
        return {}

    def __wrap_url_with_api_key(self, url: str) -> str:
        if self.__client_mode is not HTTPClientMode.V0:
            return url
        return f"{url}?api_key={self.__api_key}"

    def __ensure_v1_client_mode(self) -> None:
        if self.__client_mode is not HTTPClientMode.V1:
            raise WrongClientModeError("Use client mode `v1` to run this operation.")


def _determine_client_downsizing_parameters(
    client_downsizing_disabled: bool,
    model_description: Optional[ModelDescription],
    default_max_input_size: int,
) -> Tuple[Optional[int], Optional[int]]:
    if client_downsizing_disabled:
        return None, None
    if (
        model_description is None
        or model_description.input_height is None
        or model_description.input_width is None
    ):
        return default_max_input_size, default_max_input_size
    return model_description.input_height, model_description.input_width


def _determine_client_mode(api_url: str) -> HTTPClientMode:
    if "roboflow.com" in api_url:
        return HTTPClientMode.V0
    return HTTPClientMode.V1


def _ensure_model_is_selected(model_id: Optional[str]) -> None:
    if model_id is None:
        raise ModelNotSelectedError("No model was selected to be used.")
