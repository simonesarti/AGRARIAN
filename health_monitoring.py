from src.configs.health_monitoring import check_health_monitoring_args
from src.configs.drone import check_drone_args
from src.utils import read_yaml_config
from src.health_monitoring.health_monitoring import perform_health_monitoring
# from src.health_monitoring.health_monitoring_parallel import perform_health_monitoring


def main():

    # Read input YAML config file and transform it into dict
    input_args = read_yaml_config("configs/health_monitoring/input.yaml")
    # Check validity of arguments
    input_args = check_health_monitoring_args(input_args)   # TODO: implement

    # TODO this will be passed through the container either as env variable or volumes (to remove later, to check)
    # -------------------------------------------------
    # str: Data source (a video) for in-danger analysis.
    input_args["source"] = '/archive/group/ai/datasets/AGRARIAN/MAICH_v1/DJI_20241024104935_0008_D.MP4'
    # str: Drone metadata file (.srt)
    input_args["flight_data"] = '/archive/group/ai/datasets/AGRARIAN/MAICH_v1/DJI_20241024104935_0008_D.SRT'

    # -------------------------------------------------

    # Read drone YAML config file and transform it into dict
    drone_args = read_yaml_config("configs/drone_specs.yaml")
    # Check validity of arguments
    drone_args = check_drone_args(drone_args)

    output_args = read_yaml_config("configs/health_monitoring/output.yaml")
    tracking_args = read_yaml_config("configs/health_monitoring/tracker.yaml")
    anomaly_detection_args = read_yaml_config("configs/health_monitoring/anomaly_detector.yaml")

    print("PERFORMING HEALTH MONITORING WITH THE FOLLOWING ARGUMENTS:")
    print("Input arguments")
    print(input_args)
    print("Output arguments")
    print(output_args)
    print("Tracker arguments")
    print(tracking_args)
    print("Anomaly detection arguments")
    print(anomaly_detection_args)
    print("Drone arguments")
    print(drone_args)
    print("\n")

    perform_health_monitoring(
        input_args=input_args,
        output_args=output_args,
        tracking_args=tracking_args,
        anomaly_detection_args=anomaly_detection_args,
        drone_args=drone_args,
    )


if __name__ == "__main__":
    main()
