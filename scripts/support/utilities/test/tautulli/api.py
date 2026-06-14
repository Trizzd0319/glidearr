# beta/managers/services/tautulli/api.py

import json

import requests

from scripts.managers.factories.cache import GlobalCacheManager
from scripts.managers.factories.config import ConfigManager
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class TautulliAPI:
    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, cache=None):
        self.logger = logger or LoggerManager()
        self.config = config or ConfigManager(self.logger)
        self.cache = cache or GlobalCacheManager(logger=self.logger, config=self.config)

        tautulli_cfg = self.config.get("tautulli", {})
        self.base_url = tautulli_cfg.get("base_url")
        self.api_key = tautulli_cfg.get("api")

        if not self.base_url or not self.api_key:
            self.logger.log_error("❌ Tautulli base_url or API key is missing in config.")
            raise ValueError("Invalid Tautulli configuration.")

        self.logger.log_debug(f"✅ TautulliAPI initialized: {self.base_url}")

    @LoggerManager().log_function_entry
    @timeit("validate")
    def validate(self):
        response = self._make_request("get_server_friendly_name")
        self.logger.log_debug(f"📦 Tautulli validator raw response: {response}")

        if response:
            self.logger.log_info(f"✅ Tautulli connected: {response}")
            return True

        self.logger.log_warning("⚠️ Tautulli API validator failed.")
        return False

    @LoggerManager().log_function_entry
    @timeit("_make_request")
    def _make_request(self, cmd, params=None):
        from urllib.parse import urlencode

        url = f"{self.base_url}/api/v2"
        payload = {
            "apikey": self.api_key,
            "cmd": cmd
        }

        if params:
            payload.update(params)

        full_url = f"{url}?{urlencode(payload)}"
        # self.logger.log_info(f"🌐 Requesting Tautulli API URL: {full_url}")

        try:
            response = requests.get(url, params=payload, timeout=10)
            response.raise_for_status()
            json_response = response.json()

            self.logger.log_debug(f"📦 Tautulli JSON Response:\n{json.dumps(json_response, indent=2)[:1000]}...")

            if json_response.get("response", {}).get("result") != "success":
                self.logger.log_warning(f"⚠️ Tautulli API returned failure for cmd: {cmd}")
                return []

            return self._unwrap_data(json_response, cmd=cmd)

        except Exception as e:
            self.logger.log_error(f"❌ Tautulli request failed (cmd={cmd}): {e}")
            return []

    @LoggerManager().log_function_entry
    @timeit("_unwrap_data")
    def _unwrap_data(self, response, cmd=None):
        """
        Recursively unwraps nested 'data' blocks from a Tautulli API response.

        Args:
            response (dict): The raw response from the Tautulli API.
            cmd (str): Optional Tautulli command string for logging.

        Returns:
            list | dict | str | None: The innermost data payload.
        """
        if not isinstance(response, dict):
            self.logger.log_warning(f"⚠️ Response was not a dictionary (cmd={cmd}): {type(response)}")
            return None

        data_block = response
        depth = 0

        while isinstance(data_block, dict) and "data" in data_block:
            data_block = data_block["data"]
            depth += 1

        if isinstance(data_block, list):
            self.logger.log_info(f"📦 Unwrapped {len(data_block)} records from Tautulli command: {cmd} (depth={depth})")
            return data_block
        elif isinstance(data_block, dict):
            self.logger.log_info(f"📦 Unwrapped object from Tautulli command: {cmd} (depth={depth})")
            return data_block
        else:
            preview = str(data_block)[:200] if data_block else "None"
            self.logger.log_warning(
                f"⚠️ Unhandled structure at end of unwrap for cmd={cmd} (depth={depth}) — preview: {preview}")
            return None
