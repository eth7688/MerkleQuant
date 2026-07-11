from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


@dataclass(frozen=True)
class ExperimentSpec:
    name: str
    rules: dict
    data_version: str
    train: tuple[int, int]
    validation: tuple[int, int]
    test: tuple[int, int]
    primary_metric: str = "mean_r"
    minimum_core_trades: int = 200
    minimum_filter_samples: int = 50

    def validate(self) -> "ExperimentSpec":
        periods = (self.train, self.validation, self.test)
        if any(start >= end for start, end in periods):
            raise ValueError("invalid_period")
        if self.train[1] > self.validation[0] or self.validation[1] > self.test[0]:
            raise ValueError("period_overlap")
        if not self.rules or not self.data_version:
            raise ValueError("experiment_not_frozen")
        return self

    def _base_payload(self) -> dict:
        self.validate()
        payload = asdict(self)
        for key in ("train", "validation", "test"):
            payload[key] = list(payload[key])
        return payload

    def manifest_hash(self) -> str:
        raw = json.dumps(self._base_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def freeze(self) -> str:
        payload = self._base_payload()
        payload["manifest_hash"] = self.manifest_hash()
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
