from __future__ import annotations

import httpx
import logging
import typing
from datetime import datetime, timedelta

from ares.core.tool import BaseTool, ToolContext, ToolResult

if typing.TYPE_CHECKING:
    pass

try:
    import caldav
    _HAVE_CALDAV = True
except ImportError:
    _HAVE_CALDAV = False

logger = logging.getLogger(__name__)

# Open-Meteo base URL as a module constant for testability
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather code to text mapping (common codes)
WMO_CODES = {
    0: "Clear sky",
    1: "Partly cloudy",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Fog",
    51: "Drizzle",
    53: "Drizzle",
    55: "Drizzle",
    61: "Rain",
    63: "Rain",
    65: "Rain",
    71: "Snow",
    73: "Snow",
    75: "Snow",
    77: "Snow",
    80: "Rain showers",
    81: "Rain showers",
    82: "Rain showers",
    95: "Thunderstorm",
    96: "Thunderstorm",
    99: "Thunderstorm",
}


class GetWeather(BaseTool):
    """Get current or forecasted weather using Open-Meteo public API."""

    name = "get_weather"
    description = "Get current, today's, or tomorrow's weather forecast using your location."
    keywords = ("weather", "rain", "temperature", "forecast", "outside", "umbrella")
    parameters = {
        "type": "object",
        "properties": {
            "when": {
                "type": "string",
                "enum": ["now", "today", "tomorrow"],
                "default": "now",
                "description": "Which time period: 'now' (current), 'today', or 'tomorrow'"
            }
        }
    }
    core = False

    def __init__(self, latitude: float, longitude: float) -> None:
        """Initialize with location coordinates."""
        self.latitude = latitude
        self.longitude = longitude

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Fetch weather from Open-Meteo API."""
        when = kwargs.get("when", "now")

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    OPEN_METEO_URL,
                    params={
                        "latitude": self.latitude,
                        "longitude": self.longitude,
                        "current": "temperature_2m,weather_code,wind_speed_10m",
                        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
                        "timezone": "auto",
                        "forecast_days": 2,
                    }
                )
                if response.status_code != 200:
                    return ToolResult(
                        False,
                        f"Weather unavailable: HTTP {response.status_code}"
                    )
                data = response.json()
        except Exception as e:
            logger.exception(f"Weather API error: {e}")
            return ToolResult(False, f"Weather unavailable: {e}")

        try:
            if when == "now":
                current = data.get("current", {})
                temp = current.get("temperature_2m", "?")
                code = current.get("weather_code", -1)
                desc = WMO_CODES.get(code, "Unknown")
                wind = current.get("wind_speed_10m", "?")
                return ToolResult(
                    True,
                    f"Now: {temp}°, {desc}. Wind {wind} km/h."
                )
            elif when == "today":
                daily = data.get("daily", {})
                temps = daily.get("temperature_2m_max", [None, None])
                temp_min = daily.get("temperature_2m_min", [None, None])
                precip = daily.get("precipitation_probability_max", [None, None])
                code = daily.get("weather_code", [None, None])
                desc = WMO_CODES.get(code[0] if code else -1, "Unknown")
                return ToolResult(
                    True,
                    f"Today: {temps[0]}°/{temp_min[0]}°, {desc}. Precip {precip[0]}%."
                )
            elif when == "tomorrow":
                daily = data.get("daily", {})
                temps = daily.get("temperature_2m_max", [None, None])
                temp_min = daily.get("temperature_2m_min", [None, None])
                precip = daily.get("precipitation_probability_max", [None, None])
                code = daily.get("weather_code", [None, None])
                desc = WMO_CODES.get(code[1] if code and len(code) > 1 else -1, "Unknown")
                return ToolResult(
                    True,
                    f"Tomorrow: {temps[1] if len(temps) > 1 else '?'}°/{temp_min[1] if len(temp_min) > 1 else '?'}°, {desc}. Precip {precip[1] if len(precip) > 1 else '?'}%."
                )
        except Exception as e:
            logger.exception(f"Weather parse error: {e}")
            return ToolResult(False, f"Weather unavailable: {e}")

        return ToolResult(False, "Weather unavailable: Unknown error")


class GetCalendar(BaseTool):
    """Read calendar events from a CalDAV server."""

    name = "get_calendar"
    description = "Retrieve your calendar events for the next N days."
    keywords = ("calendar", "events", "schedule", "appointments", "agenda", "meeting")
    parameters = {
        "type": "object",
        "properties": {
            "days_ahead": {
                "type": "integer",
                "default": 1,
                "description": "Number of days to look ahead (default 1)"
            }
        }
    }
    core = False

    def __init__(self, url: str, username: str, password: str) -> None:
        """Initialize with CalDAV connection details."""
        self.url = url
        self.username = username
        self.password = password

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Fetch calendar events from CalDAV."""
        if not _HAVE_CALDAV or not self.url:
            return ToolResult(False, "Calendar is not configured.")

        days_ahead = kwargs.get("days_ahead", 1)

        try:
            client = caldav.DAVClient(
                url=self.url,
                username=self.username,
                password=self.password
            )
            principal = client.principal()
            calendars = principal.calendars()

            now = datetime.now()
            end_date = now + timedelta(days=days_ahead)

            events = []
            for cal in calendars:
                search_results = cal.date_search(
                    start=now.date(),
                    end=end_date.date()
                )
                for event in search_results:
                    try:
                        summary = event.vobject_instance.vevent.summary.value
                        dtstart = event.vobject_instance.vevent.dtstart.value
                        events.append(f"{dtstart}: {summary}")
                    except Exception:
                        pass

            if events:
                return ToolResult(True, "\n".join(events))
            else:
                return ToolResult(True, "No events scheduled.")

        except Exception as e:
            logger.exception(f"Calendar error: {e}")
            return ToolResult(False, f"Calendar error: {e}")


class AddCalendarEvent(BaseTool):
    """Create a new calendar event on a CalDAV server."""

    name = "add_calendar_event"
    description = "Create a new calendar event with title, start time, and optional end time and description."
    keywords = ("calendar", "add", "create", "event", "appointment", "schedule", "book")
    parameters = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Event title"
            },
            "start": {
                "type": "string",
                "description": "Start time in ISO8601 format (e.g., '2026-07-15T14:00:00')"
            },
            "end": {
                "type": "string",
                "description": "End time in ISO8601 format (optional)"
            },
            "description": {
                "type": "string",
                "description": "Event description (optional)"
            }
        },
        "required": ["title", "start"]
    }
    core = False

    def __init__(self, url: str, username: str, password: str) -> None:
        """Initialize with CalDAV connection details."""
        self.url = url
        self.username = username
        self.password = password

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Create a calendar event via CalDAV."""
        if not _HAVE_CALDAV or not self.url:
            return ToolResult(False, "Calendar is not configured.")

        title = kwargs.get("title")
        start = kwargs.get("start")
        end = kwargs.get("end")
        description = kwargs.get("description", "")

        if not title or not start:
            return ToolResult(False, "title and start are required.")

        try:
            client = caldav.DAVClient(
                url=self.url,
                username=self.username,
                password=self.password
            )
            principal = client.principal()
            calendars = principal.calendars()

            if not calendars:
                return ToolResult(False, "No calendars found.")

            # Use the first calendar
            cal = calendars[0]

            # Build iCalendar VEVENT
            ical_str = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//ARES//Calendar Event//EN
BEGIN:VEVENT
UID:{datetime.now().isoformat()}@ares
DTSTART:{start.replace('-', '').replace(':', '').replace('.', '')}Z
"""
            if end:
                ical_str += f"DTEND:{end.replace('-', '').replace(':', '').replace('.', '')}Z\n"

            ical_str += f"""SUMMARY:{title}
"""
            if description:
                ical_str += f"DESCRIPTION:{description}\n"

            ical_str += """END:VEVENT
END:VCALENDAR"""

            cal.add_event(ical_str)
            return ToolResult(True, "Event added.")

        except Exception as e:
            logger.exception(f"Calendar add error: {e}")
            return ToolResult(False, f"Calendar error: {e}")


def build_time_tools(config: dict) -> list[BaseTool]:
    """Factory to build time tools from configuration."""
    return [
        GetWeather(
            config.get("latitude", 0.0),
            config.get("longitude", 0.0)
        ),
        GetCalendar(
            config.get("caldav_url", ""),
            config.get("caldav_username", ""),
            config.get("caldav_password", "")
        ),
        AddCalendarEvent(
            config.get("caldav_url", ""),
            config.get("caldav_username", ""),
            config.get("caldav_password", "")
        ),
    ]
