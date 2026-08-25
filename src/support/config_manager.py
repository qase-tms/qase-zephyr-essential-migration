import json
import os


class ConfigManager:

    def __init__(self, config_file="./config.json", env_vars_prefix="QASE_"):
        self.config_file = config_file
        self.env_vars_prefix = env_vars_prefix
        self.config = {}

    def load_config(self):
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r") as file:
                    self.config = json.load(file)
        except Exception as e:
            print(f"⚠️  Failed to load config from file {self.config_file}: {e}")

    def get(self, key, default=None):
        """Dot-path lookup like ``qase.host``. Missing keys return ``default``."""
        keys = key.split(".")
        config = self.config
        for k in keys[:-1]:
            if not isinstance(config, dict) or k not in config:
                return default
            config = config[k]
        if not isinstance(config, dict):
            return default
        last = keys[-1]
        if last not in config:
            return default
        return config[last]

    def _set_config(self, key, value):
        keys = key.split(".")
        config = self.config
        for key in keys[:-1]:
            config = config.setdefault(key, {})
        config[keys[-1]] = value

    def build_config(self):
        """No interactive builder, copy config.example.json to config.json and
        run ``python preflight.py`` to validate it (never blocks a run with input())."""
        print(
            "No config found. Copy config.example.json to config.json, fill in your "
            "tokens, then run `python preflight.py`."
        )
