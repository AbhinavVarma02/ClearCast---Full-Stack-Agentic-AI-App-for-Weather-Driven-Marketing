"""Deterministic stand-in for the OpenAI Chat Completions API.

``ScriptedOpenAI`` answers real HTTP requests made by ``langchain-openai`` (via
an httpx MockTransport or the small server in ``fake_openai_server``), so tests
exercise production code: tool binding, tool-call parsing, strict JSON-schema
structured output, and token-usage metadata. Behaviour is scripted per
scenario; nothing here is a claim about real model quality.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

TONE_COPY = {
    "Friendly": "Stop by and treat yourself",
    "Urgent": "Spots are limited, book now",
    "Playful": "Weather says yes, so do we",
    "Premium": "An elevated moment, made for today",
}


@dataclass
class Block:
    oid: str
    start: datetime
    temp: float | None
    feels: float | None
    pop: float | None
    wind: float | None
    aqi: int | None
    conditions: str
    eligible: bool
    activation: list[tuple[datetime, datetime]]


def _num(text: str) -> float | None:
    text = text.strip()
    return None if text in {"", "n/a"} else float(text)


def parse_evidence(message: str) -> list[Block]:
    """Parse the drafting prompt's evidence table (the same text the real model sees)."""
    blocks = []
    for line in message.splitlines():
        if not line.startswith("fc-"):
            continue
        parts = [part.strip() for part in line.split(" | ")]
        oid, start_text, temp, feels, pop, wind, aqi, conditions, status = parts[:9]
        start = datetime.fromisoformat(start_text.split(" ", 1)[1])
        activation = []
        match = re.search(r"activation ([^)]*)\)", status)
        if status.startswith("ELIGIBLE") and match:
            for span in match.group(1).split(", "):
                begin, end = span.split("-")
                a = datetime.combine(start.date(), datetime.strptime(begin, "%H:%M").time())
                if a < start:
                    a += timedelta(days=1)
                b = datetime.combine(a.date(), datetime.strptime(end, "%H:%M").time())
                if b <= a:
                    b += timedelta(days=1)
                activation.append((a, b))
        blocks.append(
            Block(
                oid=oid,
                start=start,
                temp=_num(temp),
                feels=_num(feels),
                pop=_num(pop),
                wind=_num(wind),
                aqi=None if aqi == "n/a" else int(aqi),
                conditions=conditions,
                eligible=status.startswith("ELIGIBLE"),
                activation=activation,
            )
        )
    return blocks


def _brief_value(text: str, label: str) -> str:
    match = re.search(rf"^- ?{label}: (.*)$", text, re.M) or re.search(rf"^{label}: (.*)$", text, re.M)
    return match.group(1).strip() if match else ""


def _claims(blocks: list[Block]) -> dict:
    temps = [b.temp for b in blocks]
    winds = [b.wind for b in blocks]
    aqis = [b.aqi for b in blocks]
    return {
        "temperature_min_f": min(temps) if None not in temps else None,
        "temperature_max_f": max(temps) if None not in temps else None,
        "precipitation_probability_max_pct": max(b.pop for b in blocks)
        if all(b.pop is not None for b in blocks)
        else None,
        "wind_speed_max_mph": max(winds) if None not in winds else None,
        "aqi_max": max(aqis) if None not in aqis else None,
        "conditions_summary": ", ".join(dict.fromkeys(b.conditions for b in blocks)),
    }


def _window(blocks: list[Block], start: datetime, end: datetime, business: str, tone: str) -> dict:
    claims = _claims(blocks)
    first = blocks[0]
    return {
        "title": f"{start:%A} {start:%H:%M} {business.lower()} moment",
        "observation_ids": [b.oid for b in blocks],
        "start_local": f"{start:%Y-%m-%dT%H:%M}",
        "end_local": f"{end:%Y-%m-%dT%H:%M}",
        "claimed_conditions": claims,
        "weather_reasoning": (
            f"Forecast shows {first.temp:g}°F with a {first.pop:g}% chance of rain "
            f"and {first.conditions}, which fits the {business.lower()} brief."
            if first.temp is not None and first.pop is not None
            else f"Forecast shows {first.conditions}, which fits the brief."
        ),
        "marketing_hypothesis": (
            f"We hypothesize that {first.conditions} at this time may prompt more visits to a {business.lower()}."
        ),
        "ad_copy": [
            f"{TONE_COPY.get(tone, TONE_COPY['Friendly'])}: {first.conditions} ahead.",
            f"Make the most of {start:%A} {start:%H:%M}.",
        ],
        "risks": [
            {
                "risk": "Conditions can change before the window.",
                "mitigation": "Recheck the forecast on the morning of the campaign.",
                "observation_ids": [first.oid],
            }
        ],
    }


def grounded_draft(message: str, *, pick_ineligible: bool = False) -> dict:
    """Pick up to two non-overlapping windows from the evidence and copy values exactly."""
    blocks = parse_evidence(message)
    business = _brief_value(message, "Business type") or "business"
    tone = _brief_value(message, "Tone") or "Friendly"
    windows = []
    if pick_ineligible:
        candidates = [b for b in blocks if not b.eligible]
        if candidates:
            b = candidates[0]
            windows.append(_window([b], b.start, b.start + timedelta(hours=2), business, tone))
    eligible = [b for b in blocks if b.eligible]
    used_until: datetime | None = None
    for index, block in enumerate(eligible):
        if len(windows) >= 2:
            break
        if used_until is not None and block.start < used_until:
            continue
        span = next(((a, b) for a, b in block.activation if b - a >= timedelta(hours=1)), None)
        if span is None:
            continue
        chosen = [block]
        start, end = span
        # Prefer a two-block window when the next block continues the activation.
        following = eligible[index + 1] if index + 1 < len(eligible) else None
        if (
            not windows
            and following
            and following.start - block.start == timedelta(hours=3)
            and following.activation
            and following.activation[0][0] == end
        ):
            chosen.append(following)
            end = following.activation[0][1]
        windows.append(_window(chosen, start, end, business, tone))
        used_until = start + timedelta(days=1)
    return {
        "strategy_summary": "Focus spend on the windows below; each is tied to cited forecast blocks.",
        "windows": windows,
        "overall_risks": [],
    }


def _mutate(draft: dict, policy: str, message: str) -> dict | str:
    if not draft["windows"]:
        return draft
    first = draft["windows"][0]
    if policy == "invented_id":
        first["observation_ids"] = ["fc-20991231T0000Z"]
    elif policy == "wrong_temperature":
        first["claimed_conditions"]["temperature_max_f"] = (first["claimed_conditions"]["temperature_max_f"] or 0) + 15
    elif policy == "outside_window":
        end = datetime.fromisoformat(first["end_local"]) + timedelta(hours=4)
        first["end_local"] = f"{end:%Y-%m-%dT%H:%M}"
    elif policy == "past_window":
        start = datetime.fromisoformat(first["start_local"]) - timedelta(days=3)
        first["start_local"] = f"{start:%Y-%m-%dT%H:%M}"
    elif policy == "kpi_claim":
        first["marketing_hypothesis"] = "This campaign will increase sales by 25% and deliver a strong ROI."
    elif policy == "unhedged":
        first["marketing_hypothesis"] = "Customers buy more on days like this."
    elif policy == "unit_confusion":
        pct = first["claimed_conditions"]["precipitation_probability_max_pct"]
        first["claimed_conditions"]["precipitation_probability_max_pct"] = round(pct / 100, 2) if pct else pct
    elif policy == "invented_number":
        first["weather_reasoning"] = "A heat spike to 131°F makes this the standout window."
    elif policy == "cite_current":
        first["observation_ids"] = [m for m in re.findall(r"\bcw-\d{8}T\d{4}Z\b", message)][:1] or ["cw-x"]
    return draft


@dataclass
class ScriptedOpenAI:
    """Scripted behaviour for the agent (tool-calling) and drafting calls.

    ``draft_policies`` lists the drafting behaviour per attempt; the last entry
    repeats. Policies: grounded, ineligible_choice, malformed, missing_fields,
    and the mutations handled by ``_mutate``.
    """

    agent_policy: str = "standard"  # standard | skip_tools | loop_tools | always_air
    draft_policies: list[str] = field(default_factory=lambda: ["grounded"])
    failure: str | None = None  # model_500 | model_timeout | model_401 | draft_500
    requests: list[dict] = field(default_factory=list)
    draft_calls: int = 0
    agent_calls: int = 0

    # -- request handling -----------------------------------------------------
    def respond(self, body: dict) -> tuple[int, dict]:
        self.requests.append(body)
        is_draft = "response_format" in body
        if self.failure == "model_500" or (self.failure == "draft_500" and is_draft):
            return 500, {"error": {"message": "scripted server error", "type": "server_error"}}
        if self.failure == "model_401":
            return 401, {"error": {"message": "scripted auth error", "type": "invalid_request_error"}}
        message = self._draft(body) if is_draft else self._agent(body)
        prompt_tokens = len(json.dumps(body["messages"])) // 4
        completion_tokens = max(1, len(json.dumps(message)) // 4)
        return 200, {
            "id": f"chatcmpl-fake-{len(self.requests)}",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "gpt-4o-mini"),
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.failure == "model_timeout":
            raise httpx.ReadTimeout("scripted timeout", request=request)
        status, payload = self.respond(json.loads(request.content))
        return httpx.Response(status, json=payload)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def chat_model(self, **kwargs):
        from langchain_openai import ChatOpenAI

        transport = self.transport()
        return ChatOpenAI(
            model="gpt-4o-mini",
            api_key="sk-offline-fixture-not-a-real-key",
            max_retries=0,
            timeout=10,
            http_client=httpx.Client(transport=transport),
            http_async_client=httpx.AsyncClient(transport=transport),
            **kwargs,
        )

    # -- agent (tool-calling) ---------------------------------------------------
    def _agent(self, body: dict) -> dict:
        self.agent_calls += 1
        messages = body["messages"]
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
        brief = messages[last_user]["content"]
        turn = messages[last_user + 1 :]
        names = {}
        for m in turn:
            for call in m.get("tool_calls") or []:
                names[call["id"]] = call["function"]["name"]
        results: dict[str, list[str]] = {}
        for m in turn:
            if m["role"] == "tool":
                results.setdefault(names.get(m.get("tool_call_id"), "?"), []).append(m["content"])

        def call(name: str, args: dict, index: int) -> dict:
            return {
                "id": f"call_{self.agent_calls}_{index}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }

        if self.agent_policy == "skip_tools":
            return {"role": "assistant", "content": "Analyst notes: no data retrieved."}
        if self.agent_policy == "loop_tools":
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [call("geocode_city", {"city": "Loopville"}, 0)],
            }
        location = re.search(r"^Location: (.*)$", brief, re.M).group(1)
        geo = results.get("geocode_city", [])
        if not geo:
            return {"role": "assistant", "content": None, "tool_calls": [call("geocode_city", {"city": location}, 0)]}
        try:
            coords = json.loads(geo[-1])
            lat, lon = coords["lat"], coords["lon"]
        except (ValueError, KeyError):
            if len(geo) < 2:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [call("geocode_city", {"city": location}, 0)],
                }
            return {"role": "assistant", "content": "Analyst notes: the location could not be resolved."}
        if "get_forecast" not in results:
            wanted = ["get_forecast", "get_current_weather"]
            if self.agent_policy == "always_air" or "Air quality evidence is required" in brief:
                wanted.append("get_air_quality")
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [call(name, {"lat": lat, "lon": lon}, i) for i, name in enumerate(wanted)],
            }
        return {
            "role": "assistant",
            "content": "Analyst notes: mornings and evenings stand out; see the forecast blocks for details.",
        }

    # -- drafting (structured output) -------------------------------------------
    def _draft(self, body: dict) -> dict:
        policy = self.draft_policies[min(self.draft_calls, len(self.draft_policies) - 1)]
        self.draft_calls += 1
        # The drafting context is the first user message; repairs append feedback.
        message = next(m["content"] for m in body["messages"] if m["role"] == "user")
        if policy == "malformed":
            return {"role": "assistant", "content": '{"strategy_summary": "unterminated'}
        if policy == "missing_fields":
            return {"role": "assistant", "content": json.dumps({"strategy_summary": "No windows field."})}
        draft = grounded_draft(message, pick_ineligible=policy == "ineligible_choice")
        draft = _mutate(draft, policy, message)
        return {"role": "assistant", "content": json.dumps(draft)}
