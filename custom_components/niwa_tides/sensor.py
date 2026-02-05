"""Support for the NIWA Tides API."""
from datetime import timedelta
import logging
import time
import datetime
import traceback

import math

import requests
import voluptuous as vol

from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import (
    ATTR_ATTRIBUTION,
    CONF_API_KEY,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_NAME,
    CONF_ENTITY_ID,
    UnitOfLength
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity

_LOGGER = logging.getLogger(__name__)

ATTRIBUTION = "Data provided by NIWA"
DEFAULT_NAME = "NIWA Tides"
DEFAULT_ENTITY_ID = "niwa_tides"
ICON = "mdi:waves"
ATTR_LAST_TIDE_LEVEL = "last_tide_level"
ATTR_LAST_TIDE_TIME = "last_tide_time"
ATTR_LAST_TIDE_HOURS = "last_tide_hours"
ATTR_NEXT_TIDE_LEVEL = "next_tide_level"
ATTR_NEXT_TIDE_TIME = "next_tide_time"
ATTR_NEXT_TIDE_HOURS = "next_tide_hours"
ATTR_NEXT_HIGH_TIDE_LEVEL = "next_high_tide_level"
ATTR_NEXT_HIGH_TIDE_TIME = "next_high_tide_time"
ATTR_NEXT_HIGH_TIDE_HOURS = "next_high_tide_hours"
ATTR_NEXT_LOW_TIDE_LEVEL = "next_low_tide_level"
ATTR_NEXT_LOW_TIDE_TIME = "next_low_tide_time"
ATTR_NEXT_LOW_TIDE_HOURS = "next_low_tide_hours"
ATTR_TIDE_PERCENT = "tide_percent"
ATTR_TIDE_PHASE = "tide_phase"
UPCOMING_TIDES = "upcoming_tides"
ATTR_SAFE_WINDOW_START = "safe_window_start"
ATTR_SAFE_WINDOW_END = "safe_window_end"
ATTR_NEXT_SAFE_WINDOW = "next_safe_window"
ATTR_MUST_RETURN_BY = "must_return_by"

SCAN_INTERVAL = timedelta(seconds=300)  # every 5 minutes

# Boat ramp configuration - time windows around low tide
HOURS_BEFORE_LOW_TIDE = 2  # Can launch 2 hours before low tide
HOURS_AFTER_LOW_TIDE = 2   # Can launch 2 hours after low tide

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_API_KEY): cv.string,
        vol.Optional(CONF_LATITUDE): cv.latitude,
        vol.Optional(CONF_LONGITUDE): cv.longitude,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_ENTITY_ID, default=DEFAULT_ENTITY_ID): cv.string,
    }
)


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the NiwaTidesInfo sensor."""
    try:
        name = config.get(CONF_NAME)
        entity_id = config[CONF_ENTITY_ID]

        lat = config.get(CONF_LATITUDE, hass.config.latitude)
        lon = config.get(CONF_LONGITUDE, hass.config.longitude)
        key = config.get(CONF_API_KEY)

        _LOGGER.info("Setting up NIWA Tides sensor: %s", name)

        if None in (lat, lon):
            _LOGGER.error("Latitude or longitude not set in Home Assistant config")
            return

        # Normalise float precision for NIWA API
        try:
            lat = round(float(lat), 5)
            lon = round(float(lon), 5)
        except (TypeError, ValueError):
            _LOGGER.error("Invalid latitude or longitude values: lat=%s lon=%s", lat, lon)
            return

        if not (-90 <= lat <= 90):
            _LOGGER.error("Latitude out of range: %s", lat)
            return

        if not ((165 <= lon <= 180) or (-180 <= lon <= -175)):
            _LOGGER.error("Longitude out of range for NIWA tides: %s", lon)
            return

        tides = NiwaTidesInfoSensor(name, entity_id, lat, lon, key)
        boat_out = BoatLaunchSensor(name, entity_id, tides)
        boat_in = BoatReturnSensor(name, entity_id, tides)

        add_entities([tides, boat_out, boat_in])

        tides.update()
        if tides.data is None:
            _LOGGER.error("Unable to retrieve tides data")
        else:
            _LOGGER.info("NIWA Tides sensor setup completed successfully")

    except Exception as e:
        _LOGGER.error("Error setting up NIWA Tides sensor: %s", e, exc_info=True)


class NiwaTidesInfoSensor(RestoreEntity):
    """Representation of a NiwaTidesInfo sensor."""

    def __init__(self, name, entity_id, lat, lon, key):
        """Initialize the sensor."""
        self._name = name
        self._entity_id = entity_id
        self._lat = lat
        self._lon = lon
        self._key = key
        self.data = None
        self.tide_percent = None
        self.tide_phase = None
        self.current_tide_level = None
        self.last_tide = None
        self.next_tide = None
        self.next_high_tide = None
        self.next_low_tide = None
        self.upcoming_tides = []
        self.last_update_at = None

        # Boat launch timing
        self.safe_to_launch = False
        self.safe_window_start = None
        self.safe_window_end = None
        self.next_safe_window_start = None
        self.must_return_by = None

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique ID of the sensor."""
        return self._entity_id

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.data is not None

    @property
    def icon(self):
        """Return sensor icon."""
        return ICON

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return UnitOfLength.METERS

    @property
    def extra_state_attributes(self):
        """Return the state attributes of this device."""
        try:
            if self.last_update_at is None:
                self.last_update_at = datetime.datetime.now()

            attr = {
                ATTR_ATTRIBUTION: ATTRIBUTION,
                ATTR_LAST_TIDE_LEVEL: self.last_tide.value if self.last_tide is not None else None,
                ATTR_LAST_TIDE_TIME: self.last_tide.time if self.last_tide is not None else None,
                ATTR_LAST_TIDE_HOURS: difference_in_hours(self.last_tide.time, self.last_update_at) if self.last_tide is not None else None,
                ATTR_NEXT_TIDE_LEVEL: self.next_tide.value if self.next_tide is not None else None,
                ATTR_NEXT_TIDE_TIME: self.next_tide.time if self.next_tide is not None else None,
                ATTR_NEXT_TIDE_HOURS: difference_in_hours(self.last_update_at, self.next_tide.time) if self.next_tide is not None else None,
                ATTR_NEXT_HIGH_TIDE_LEVEL: self.next_high_tide.value if self.next_high_tide is not None else None,
                ATTR_NEXT_HIGH_TIDE_TIME: self.next_high_tide.time if self.next_high_tide is not None else None,
                ATTR_NEXT_HIGH_TIDE_HOURS: difference_in_hours(self.last_update_at, self.next_high_tide.time) if self.next_high_tide is not None else None,
                ATTR_NEXT_LOW_TIDE_LEVEL: self.next_low_tide.value if self.next_low_tide is not None else None,
                ATTR_NEXT_LOW_TIDE_TIME: self.next_low_tide.time if self.next_low_tide is not None else None,
                ATTR_NEXT_LOW_TIDE_HOURS: difference_in_hours(self.last_update_at, self.next_low_tide.time) if self.next_low_tide is not None else None,
                ATTR_TIDE_PERCENT: self.tide_percent,
                ATTR_TIDE_PHASE: self.tide_phase,
                UPCOMING_TIDES: self.upcoming_tides,
                ATTR_SAFE_WINDOW_START: self.safe_window_start.isoformat() if self.safe_window_start else None,
                ATTR_SAFE_WINDOW_END: self.safe_window_end.isoformat() if self.safe_window_end else None,
                ATTR_NEXT_SAFE_WINDOW: self.next_safe_window_start.isoformat() if self.next_safe_window_start else None,
                ATTR_MUST_RETURN_BY: self.must_return_by.isoformat() if self.must_return_by else None,
            }
            return attr
        except Exception as e:
            _LOGGER.error("Error building state attributes: %s", e, exc_info=True)
            return {ATTR_ATTRIBUTION: ATTRIBUTION}

    @property
    def state(self):
        """Return the state of the device."""
        return self.current_tide_level

    def update(self):
        """Get the latest data from NIWA Tides API or calculate."""
        try:
            self.last_update_at = datetime.datetime.now()

            if self.data is None or self.next_tide is None or datetime.datetime.now() > self.next_tide.time:
                start = datetime.date.fromtimestamp(time.time()).isoformat()
                _LOGGER.info("Fetching tide data for %s", start)
                resource = (
                    "https://api.niwa.co.nz/tides/data?lat={}&long={}&numberOfDays=7&startDate={}"
                ).format(self._lat, self._lon, start)

                try:
                    req = requests.get(resource, timeout=10, headers={"x-apikey": self._key})

                    if req.status_code != 200:
                        _LOGGER.error("NIWA API error %s: %s", req.status_code, req.text[:300])
                        self.data = None
                        return

                    self.data = req.json()
                    req.close()

                    _LOGGER.debug("Data: %s", self.data)

                    self.calculate_tide()
                except ValueError as err:
                    _LOGGER.error("Error retrieving data from NIWA tides API: %s", err.args)
                    if 'req' in locals():
                        _LOGGER.debug("Response (%s): %s", req.status_code, req.text)
                    self.data = None
                except Exception as err:
                    _LOGGER.error("Unexpected error retrieving data from NIWA tides API: %s", err, exc_info=True)
                    self.data = None
            else:
                self.calculate_tide()
        except Exception as e:
            _LOGGER.error("Error in update method: %s", e, exc_info=True)

    def calculate_tide(self):
        """Calculate current tide level and next tides from API data."""
        try:
            if not self.data or "values" not in self.data:
                _LOGGER.warning("No tide data available to calculate")
                self.tide_percent = None
                self.current_tide_level = None
                self.last_tide = None
                self.next_tide = None
                self.next_high_tide = None
                self.next_low_tide = None
                self.tide_phase = None
                self.upcoming_tides = []
                return

            t = datetime.datetime.now()

            # Build upcoming tides list
            future = []
            try:
                for v in self.data["values"]:
                    pt = datetime.datetime.strptime(v["time"], "%Y-%m-%dT%H:%M:%SZ") \
                        .replace(tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None)
                    if pt > t:
                        future.append({"time": pt.isoformat(), "value": round(float(v["value"]), 2)})
                self.upcoming_tides = future[:14]
                _LOGGER.debug("Found %s upcoming tide events", len(self.upcoming_tides))
            except Exception as e:
                _LOGGER.error("Error building upcoming tides list: %s", e, exc_info=True)
                self.upcoming_tides = []

            last_tide = None
            next_tide = None
            next_high_tide = None
            next_low_tide = None

            for value in self.data["values"]:
                parsed_time = datetime.datetime.strptime(value["time"], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None)
                if next_tide is None:
                    if parsed_time > t:
                        next_tide = TideInfo(parsed_time, value["value"])

                        if last_tide is not None:
                            if next_tide.value > last_tide.value:
                                next_high_tide = next_tide
                            else:
                                next_low_tide = next_tide
                    else:
                        last_tide = TideInfo(parsed_time, value["value"])
                else:
                    if next_high_tide is None:
                        next_high_tide = TideInfo(parsed_time, value["value"])
                    else:
                        next_low_tide = TideInfo(parsed_time, value["value"])
                    break

            if last_tide is None or next_tide is None:
                _LOGGER.error("Could not determine last_tide and/or next_tide")
                self.tide_percent = None
                self.current_tide_level = None
                self.tide_phase = None
                return

            # Calculate current level
            tide_ratio = (1-math.cos(math.pi*(t-last_tide.time)/(next_tide.time-last_tide.time)))/2
            h = last_tide.value + (next_tide.value - last_tide.value)*tide_ratio
            h = round(h, 2)

            _LOGGER.debug("Current tide: %s. Last tide: %s. Next tide: %s", h, last_tide, next_tide)
            _LOGGER.debug("Next high tide: %s. Next low tide: %s", next_high_tide, next_low_tide)

            if last_tide.value > next_tide.value:
                tide_ratio = 1 - tide_ratio

            self.tide_percent = round(tide_ratio * 100, 0)
            self.current_tide_level = h
            self.last_tide = last_tide
            self.next_tide = next_tide
            self.next_high_tide = next_high_tide
            self.next_low_tide = next_low_tide

            if self.tide_percent < 5:
                self.tide_phase = "low"
            elif self.tide_percent > 95:
                self.tide_phase = "high"
            elif last_tide.value < next_tide.value:
                self.tide_phase = "increasing"
            else:
                self.tide_phase = "decreasing"

            # Calculate boat launch windows
            self.calculate_boat_windows()

            _LOGGER.info("Tide calculation complete - Level: %s m, Phase: %s (%s%%)",
                        self.current_tide_level, self.tide_phase, self.tide_percent)

        except Exception as e:
            _LOGGER.error("Error in calculate_tide: %s", e, exc_info=True)
            self.tide_percent = None
            self.current_tide_level = None
            self.last_tide = None
            self.next_tide = None
            self.next_high_tide = None
            self.next_low_tide = None
            self.tide_phase = None
            self.upcoming_tides = []

    def calculate_boat_windows(self):
        """Calculate safe boat launch and return windows."""
        try:
            if not self.next_low_tide:
                return

            now = datetime.datetime.now()

            # Current safe window around next low tide
            window_start = self.next_low_tide.time - timedelta(hours=HOURS_BEFORE_LOW_TIDE)
            window_end = self.next_low_tide.time + timedelta(hours=HOURS_AFTER_LOW_TIDE)

            # Check if we're currently in a safe window
            if window_start <= now <= window_end:
                self.safe_to_launch = True
                self.safe_window_start = window_start
                self.safe_window_end = window_end
                self.must_return_by = window_end
                self.next_safe_window_start = None

                # Find the next low tide after this one for next window
                for tide in self.upcoming_tides:
                    tide_time = datetime.datetime.fromisoformat(tide["time"])
                    # Find next low tide (will be after a high tide)
                    if tide_time > self.next_low_tide.time:
                        # This should be the tide after next low (likely high)
                        continue

            else:
                self.safe_to_launch = False

                if now < window_start:
                    # Next window hasn't started yet
                    self.safe_window_start = window_start
                    self.safe_window_end = window_end
                    self.next_safe_window_start = window_start
                    self.must_return_by = None
                else:
                    # We're past this window, find next low tide
                    next_low_found = False
                    for i, tide in enumerate(self.upcoming_tides):
                        tide_time = datetime.datetime.fromisoformat(tide["time"])
                        # Find the next low tide (smaller value after a high value)
                        if i > 0:
                            prev_value = self.upcoming_tides[i-1]["value"]
                            if tide["value"] < prev_value and tide_time > now:
                                next_low_time = tide_time
                                self.safe_window_start = next_low_time - timedelta(hours=HOURS_BEFORE_LOW_TIDE)
                                self.safe_window_end = next_low_time + timedelta(hours=HOURS_AFTER_LOW_TIDE)
                                self.next_safe_window_start = self.safe_window_start
                                next_low_found = True
                                break

                    if not next_low_found:
                        self.safe_window_start = None
                        self.safe_window_end = None
                        self.next_safe_window_start = None

                    self.must_return_by = None

            _LOGGER.debug("Boat windows - Safe to launch: %s, Window: %s to %s, Next: %s",
                         self.safe_to_launch, self.safe_window_start, self.safe_window_end,
                         self.next_safe_window_start)

        except Exception as e:
            _LOGGER.error("Error calculating boat windows: %s", e, exc_info=True)


class BoatLaunchSensor(BinarySensorEntity):
    """Binary sensor indicating if it's safe to launch the boat."""

    def __init__(self, name, entity_id, tide_sensor):
        """Initialize the sensor."""
        self._name = f"{name} Boat Launch"
        self._entity_id = f"{entity_id}_boat_launch"
        self._tide_sensor = tide_sensor

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique ID of the sensor."""
        return self._entity_id

    @property
    def is_on(self):
        """Return true if it's safe to launch."""
        return self._tide_sensor.safe_to_launch

    @property
    def icon(self):
        """Return the icon."""
        return "mdi:sail-boat" if self.is_on else "mdi:anchor"

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            "window_start": self._tide_sensor.safe_window_start.isoformat() if self._tide_sensor.safe_window_start else None,
            "window_end": self._tide_sensor.safe_window_end.isoformat() if self._tide_sensor.safe_window_end else None,
            "next_window": self._tide_sensor.next_safe_window_start.isoformat() if self._tide_sensor.next_safe_window_start else None,
        }

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._tide_sensor.available

    def update(self):
        """Update is handled by the tide sensor."""
        pass


class BoatReturnSensor(BinarySensorEntity):
    """Binary sensor warning when boat must return."""

    def __init__(self, name, entity_id, tide_sensor):
        """Initialize the sensor."""
        self._name = f"{name} Boat Return"
        self._entity_id = f"{entity_id}_boat_return"
        self._tide_sensor = tide_sensor

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique ID of the sensor."""
        return self._entity_id

    @property
    def is_on(self):
        """Return true if boat should return soon."""
        if not self._tide_sensor.must_return_by:
            return False

        now = datetime.datetime.now()
        time_remaining = (self._tide_sensor.must_return_by - now).total_seconds() / 3600

        # Warn if less than 30 minutes remaining
        return time_remaining < 0.5

    @property
    def icon(self):
        """Return the icon."""
        return "mdi:alert" if self.is_on else "mdi:clock-outline"

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        if not self._tide_sensor.must_return_by:
            return {}

        now = datetime.datetime.now()
        time_remaining = (self._tide_sensor.must_return_by - now).total_seconds() / 3600

        return {
            "must_return_by": self._tide_sensor.must_return_by.isoformat(),
            "hours_remaining": round(time_remaining, 1),
        }

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._tide_sensor.available

    def update(self):
        """Update is handled by the tide sensor."""
        pass


class TideInfo:
    def __init__(self, time: datetime.datetime, value: float):
        self.time = time
        self.value = value

    def __str__(self):
        return f'{self.value}m at {self.time}'


def difference_in_hours(earlier_time, later_time):
    try:
        diff = later_time - earlier_time
        return round(diff.days*24 + diff.seconds/3600, 1)
    except Exception as e:
        _LOGGER.error("Error calculating time difference: %s", e)
        return None