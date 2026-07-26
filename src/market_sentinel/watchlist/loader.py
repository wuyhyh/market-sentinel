from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from market_sentinel.domain.watchlist import WatchlistConfig


@dataclass(frozen=True)
class WatchlistValidationIssue:
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


class WatchlistConfigurationError(Exception):
    def __init__(
        self,
        config_path: Path,
        issues: tuple[WatchlistValidationIssue, ...],
    ) -> None:
        self.config_path = config_path
        self.issues = issues
        super().__init__("; ".join(issue.message for issue in issues))


class _UniqueKeySafeLoader(yaml.SafeLoader):
    def construct_mapping(
        self,
        node: MappingNode,
        deep: bool = False,
    ) -> dict[object, object]:
        self.flatten_mapping(node)
        mapping: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found a duplicate mapping key",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


class WatchlistLoader:
    def load(self, config_path: Path) -> WatchlistConfig:
        try:
            raw_text = config_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise self._error(
                config_path,
                "file_not_found",
                "$",
                "watchlist configuration does not exist",
            ) from exc
        except OSError as exc:
            raise self._error(
                config_path,
                "file_unreadable",
                "$",
                "watchlist configuration cannot be read",
            ) from exc

        try:
            raw_config = yaml.load(raw_text, Loader=_UniqueKeySafeLoader)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            location = (
                f" at line {mark.line + 1}, column {mark.column + 1}"
                if mark is not None
                else ""
            )
            raise self._error(
                config_path,
                "yaml_error",
                "$",
                f"watchlist configuration contains invalid YAML{location}",
            ) from exc

        try:
            return WatchlistConfig.model_validate(raw_config)
        except ValidationError as exc:
            issues = tuple(
                sorted(
                    (
                        WatchlistValidationIssue(
                            code=str(error["type"]),
                            path=self._format_path(error["loc"]),
                            message=str(error["msg"]),
                        )
                        for error in exc.errors(include_input=False)
                    ),
                    key=lambda issue: (issue.path, issue.code, issue.message),
                )
            )
            raise WatchlistConfigurationError(config_path, issues) from exc

    @staticmethod
    def _error(
        config_path: Path,
        code: str,
        path: str,
        message: str,
    ) -> WatchlistConfigurationError:
        return WatchlistConfigurationError(
            config_path,
            (WatchlistValidationIssue(code=code, path=path, message=message),),
        )

    @staticmethod
    def _format_path(parts: tuple[int | str, ...]) -> str:
        if not parts:
            return "$"
        rendered = "$"
        for part in parts:
            rendered += f"[{part}]" if isinstance(part, int) else f".{part}"
        return rendered

