"""WhatsApp message templates (WHATSAPP.md §5 Templates). Pure functions.

A template send is a message with ``content_type = "template"`` and

    content_attributes.template = {
        "name": "order_update", "language": "en_US",
        "parameters": {"header": [...], "body": ["Ana", "#1234"]}     # positional
                   or {"body": {"customer_name": "Ana"}}             # named
    }

``validate_and_build`` checks the parameters against the synced template's
components and returns the Graph ``components`` plus the rendered text that
the timeline shows (what the customer receives).
"""

import re
from typing import Any

from app.channels.base import PermanentChannelError

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


class TemplateError(PermanentChannelError):
    code = "template_error"


def placeholders(text: str | None) -> list[str]:
    return PLACEHOLDER.findall(text or "")


def _component(components: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((c for c in components if str(c.get("type", "")).upper() == kind), None)


def _values(
    given: Any, names: list[str], part: str, named: bool
) -> list[tuple[str, str]]:
    if not names:
        if given:
            raise TemplateError(f"The template's {part} takes no parameters")
        return []
    if named:
        if not isinstance(given, dict):
            raise TemplateError(
                f"{part}: named parameters expected ({', '.join(names)})"
            )
        missing = [n for n in names if not str(given.get(n, "")).strip()]
        if missing:
            raise TemplateError(f"{part}: missing {', '.join(missing)}")
        extra = set(given) - set(names)
        if extra:
            raise TemplateError(f"{part}: unknown {', '.join(sorted(extra))}")
        return [(n, str(given[n])) for n in names]
    if not isinstance(given, list):
        raise TemplateError(f"{part}: a list of {len(names)} values expected")
    if len(given) != len(names) or any(not str(v).strip() for v in given):
        raise TemplateError(
            f"{part}: {len(names)} parameters expected, got {len(given)}"
        )
    return [(n, str(v)) for n, v in zip(names, given, strict=True)]


def validate_and_build(
    template: dict[str, Any],
    *,
    components: list[dict[str, Any]],
    parameter_format: str,
    status: str,
) -> tuple[list[dict[str, Any]], str]:
    if status.upper() != "APPROVED":
        raise TemplateError(f"Template is {status}, not APPROVED")
    named = parameter_format == "named"
    params = template.get("parameters") or {}
    graph: list[dict[str, Any]] = []
    rendered: list[str] = []
    for part in ("header", "body"):
        comp = _component(components, part.upper())
        text = comp.get("text") if comp else None
        if (
            comp
            and part == "header"
            and str(comp.get("format", "TEXT")).upper() != "TEXT"
        ):
            media = params.get("header_media")
            if not media:
                raise TemplateError("header: media link required")
            fmt = str(comp["format"]).lower()
            graph.append(
                {"type": "header", "parameters": [{"type": fmt, fmt: {"link": media}}]}
            )
            continue
        values = _values(params.get(part), placeholders(text), part, named)
        if values:
            graph.append(
                {
                    "type": part,
                    "parameters": [
                        {
                            "type": "text",
                            "text": v,
                            **({"parameter_name": n} if named else {}),
                        }
                        for n, v in values
                    ],
                }
            )
        if text:
            mapping = dict(values)
            rendered.append(
                PLACEHOLDER.sub(lambda m: mapping.get(m.group(1), m.group(0)), text)
            )
    buttons = _component(components, "BUTTONS")
    for i, button in enumerate((buttons or {}).get("buttons") or []):
        if str(button.get("type", "")).upper() == "URL" and placeholders(
            button.get("url")
        ):
            value = (params.get("buttons") or {}).get(str(i))
            if not value:
                raise TemplateError(f"button {i}: URL parameter required")
            graph.append(
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": str(i),
                    "parameters": [{"type": "text", "text": str(value)}],
                }
            )
    footer = _component(components, "FOOTER")
    if footer and footer.get("text"):
        rendered.append(footer["text"])
    return graph, "\n\n".join(rendered)
