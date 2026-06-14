class TautulliInstanceSummaryFormatter:
    def __init__(self, api, logger):
        self.api = api
        self.logger = logger

    def format_summary(self):
        try:
            server_info = self.api.get_server_info()
            users = self.api.get_users()
            now_playing = self.api.get_activity()

            data = ((server_info.get("response") or {}).get("data") or {}) if server_info else {}

            return {
                "version": data.get("pms_version", "unknown"),
                "platform": data.get("pms_platform", "unknown"),
                "user_count": len((users.get("response") or {}).get("data") or []) if users else 0,
                "active_streams": len(
                    ((now_playing.get("response") or {}).get("data") or {}).get("sessions") or []) if now_playing else 0,
            }

        except Exception as e:
            self.logger.log_error(f"Failed to generate Tautulli summary: {e}")
            return {
                "version": "unknown",
                "platform": "unknown",
                "user_count": 0,
                "active_streams": 0,
            }
