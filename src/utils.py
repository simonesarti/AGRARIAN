import yaml


def read_yaml_config(yaml_file: str) -> dict:
    try:
        with open(yaml_file, "r") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"YAML config file not found: '{yaml_file}'")
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse YAML config '{yaml_file}': {e}")
