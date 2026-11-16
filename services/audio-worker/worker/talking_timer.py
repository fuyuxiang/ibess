class TalkingTimer:
    """Track one continuous speech interval; file-source rounds can be reset explicitly."""

    def __init__(self, threshold: float, allowed: bool):
        self.threshold = threshold
        self.allowed = allowed
        self.started_at = None
        self.last_report_at = None

    def observe(self, timestamp: float, has_speech: bool):
        if not has_speech:
            self.started_at = None
            return None
        if self.started_at is None:
            self.started_at = timestamp
        duration = timestamp - self.started_at
        if self.allowed or duration <= self.threshold:
            return None
        if self.last_report_at is None or timestamp - self.last_report_at > self.threshold:
            self.last_report_at = timestamp
            return duration
        return None

    def end_round(self):
        self.started_at = None
        self.last_report_at = None
