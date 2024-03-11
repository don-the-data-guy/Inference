from typing import Optional

import typer
from typing_extensions import Annotated

from inference_cli.lib.benchmark.dataset import PREDEFINED_DATASETS
from inference_cli.lib.benchmark_adapter import (
    run_api_speed_benchmark,
    run_python_package_speed_benchmark,
)

benchmark_app = typer.Typer(help="Commands for running inference benchmarks.")


@benchmark_app.command()
def api_speed(
    model_id: Annotated[
        str,
        typer.Option(
            "--model_id",
            "-m",
            help="Model ID in format project/version.",
        ),
    ],
    dataset_reference: Annotated[
        str,
        typer.Option(
            "--dataset_reference",
            "-d",
            help=f"Name of predefined dataset (one of {list(PREDEFINED_DATASETS.keys())}) or path to directory with images",
        ),
    ] = "coco",
    host: Annotated[
        str,
        typer.Option("--host", "-h", help="Host to run inference on."),
    ] = "http://localhost:9001",
    warm_up_requests: Annotated[
        int,
        typer.Option("--warm_up_requests", "-wr", help="Number of warm-up requests"),
    ] = 10,
    benchmark_requests: Annotated[
        int,
        typer.Option(
            "--benchmark_requests", "-br", help="Number of benchmark requests"
        ),
    ] = 1000,
    request_batch_size: Annotated[
        int,
        typer.Option("--batch_size", "-bs", help="Batch size of single request"),
    ] = 1,
    number_of_clients: Annotated[
        int,
        typer.Option(
            "--clients",
            "-c",
            help="Meaningful if `rps` not specified - number of concurrent threads that will send requests one by one",
        ),
    ] = 1,
    requests_per_second: Annotated[
        Optional[int],
        typer.Option(
            "--rps",
            "-rps",
            help="Number of requests per second to emit. If not specified - requests will be sent one-by-one by requested number of client threads",
        ),
    ] = None,
    api_key: Annotated[
        Optional[str],
        typer.Option(
            "--api-key",
            "-a",
            help="Roboflow API key for your workspace. If not given - env variable `ROBOFLOW_API_KEY` will be used",
        ),
    ] = None,
    model_configuration: Annotated[
        Optional[str],
        typer.Option(
            "--model_config", "-mc", help="Location of yaml file with model config"
        ),
    ] = None,
    output_location: Annotated[
        Optional[str],
        typer.Option(
            "--output_location",
            "-o",
            help="Location where to save the result (path to file or directory)",
        ),
    ] = None,
    enforce_legacy_endpoints: Annotated[
        bool,
        typer.Option(
            "--legacy-endpoints/--no-legacy-endpoints",
            "-L/-l",
            help="Boolean flag to decide if legacy endpoints should be used (applicable for self-hosted API benchmark)",
        ),
    ] = False,
    proceed_automatically: Annotated[
        bool,
        typer.Option(
            "--yes/--no",
            "-y/-n",
            help="Boolean flag to decide on auto `yes` answer given on user input required.",
        ),
    ] = False,
):
    if "roboflow.com" in host and not proceed_automatically:
        proceed = input(
            "This action may easily exceed your Roboflow inference credits. Are you sure? [y/N] "
        )
        if proceed.lower() != "y":
            return None
    run_api_speed_benchmark(
        model_id=model_id,
        dataset_reference=dataset_reference,
        host=host,
        warm_up_requests=warm_up_requests,
        benchmark_requests=benchmark_requests,
        request_batch_size=request_batch_size,
        number_of_clients=number_of_clients,
        requests_per_second=requests_per_second,
        api_key=api_key,
        model_configuration=model_configuration,
        output_location=output_location,
        enforce_legacy_endpoints=enforce_legacy_endpoints,
    )


@benchmark_app.command()
def python_package_speed(
    model_id: Annotated[
        str,
        typer.Option(
            "--model_id",
            "-m",
            help="Model ID in format project/version.",
        ),
    ],
    dataset_reference: Annotated[
        str,
        typer.Option(
            "--dataset_reference",
            "-d",
            help=f"Name of predefined dataset (one of {list(PREDEFINED_DATASETS.keys())}) or path to directory with images",
        ),
    ] = "coco",
    warm_up_inferences: Annotated[
        int,
        typer.Option("--warm_up_inferences", "-wi", help="Number of warm-up requests"),
    ] = 10,
    benchmark_inferences: Annotated[
        int,
        typer.Option(
            "--benchmark_requests", "-bi", help="Number of benchmark requests"
        ),
    ] = 1000,
    batch_size: Annotated[
        int,
        typer.Option("--batch_size", "-bs", help="Batch size of single request"),
    ] = 1,
    api_key: Annotated[
        Optional[str],
        typer.Option(
            "--api-key",
            "-a",
            help="Roboflow API key for your workspace. If not given - env variable `ROBOFLOW_API_KEY` will be used",
        ),
    ] = None,
    model_configuration: Annotated[
        Optional[str],
        typer.Option(
            "--model_config", "-mc", help="Location of yaml file with model config"
        ),
    ] = None,
    output_location: Annotated[
        Optional[str],
        typer.Option(
            "--output_location",
            "-o",
            help="Location where to save the result (path to file or directory)",
        ),
    ] = None,
):
    run_python_package_speed_benchmark(
        model_id=model_id,
        dataset_reference=dataset_reference,
        warm_up_inferences=warm_up_inferences,
        benchmark_inferences=benchmark_inferences,
        batch_size=batch_size,
        api_key=api_key,
        model_configuration=model_configuration,
        output_location=output_location,
    )


if __name__ == "__main__":
    benchmark_app()
