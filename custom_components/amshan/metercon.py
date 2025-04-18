"""Meter connection module."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from han import (
    common as han_type,
)
from han import (
    dlde,
    hdlc,
    meter_connection,
)
from han import (
    serial_connection_factory as han_serial,
)
from han import (
    tcp_connection_factory as han_tcp,
)
from homeassistant.components import mqtt
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .const import (
    CONF_MQTT_TOPICS,
    CONF_SERIAL_BAUDRATE,
    CONF_SERIAL_BYTESIZE,
    CONF_SERIAL_DSRDTR,
    CONF_SERIAL_PARITY,
    CONF_SERIAL_PORT,
    CONF_SERIAL_RTSCTS,
    CONF_SERIAL_STOPBITS,
    CONF_SERIAL_XONXOFF,
    CONF_TCP_HOST,
    CONF_TCP_PORT,
)

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Mapping


_LOGGER: logging.Logger = logging.getLogger(__name__)


def setup_meter_connection(
    loop: asyncio.AbstractEventLoop,
    config: Mapping[str, Any],
    measure_queue: asyncio.Queue[han_type.MeterMessageBase],
) -> meter_connection.ConnectionManager:
    """Initialize ConnectionManager using configured connection type."""
    connection_factory = get_connection_factory(loop, config, measure_queue)
    return meter_connection.ConnectionManager(connection_factory)


def get_connection_factory(
    loop: asyncio.AbstractEventLoop,
    config: Mapping[str, Any],
    measure_queue: asyncio.Queue[han_type.MeterMessageBase],
) -> meter_connection.AsyncConnectionFactory:
    """Get connection factory based on configured connection type."""

    async def tcp_connection_factory() -> meter_connection.MeterTransportProtocol:
        return await han_tcp.create_tcp_message_connection(
            measure_queue,
            loop,
            None,
            host=config[CONF_TCP_HOST],
            port=config[CONF_TCP_PORT],
        )

    async def serial_connection_factory() -> meter_connection.MeterTransportProtocol:
        return await han_serial.create_serial_message_connection(
            measure_queue,
            loop,
            None,
            url=config[CONF_SERIAL_PORT],
            baudrate=config[CONF_SERIAL_BAUDRATE],
            parity=config[CONF_SERIAL_PARITY],
            bytesize=config[CONF_SERIAL_BYTESIZE],
            stopbits=float(config[CONF_SERIAL_STOPBITS]),
            xonxoff=config[CONF_SERIAL_XONXOFF],
            rtscts=config[CONF_SERIAL_RTSCTS],
            dsrdtr=config[CONF_SERIAL_DSRDTR],
        )

    # select tcp or serial connection factory
    return (
        tcp_connection_factory if CONF_TCP_HOST in config else serial_connection_factory
    )


async def async_setup_meter_mqtt_subscriptions(
    hass: HomeAssistant,
    config: Mapping[str, Any],
    measure_queue: asyncio.Queue[han_type.MeterMessageBase],
) -> CALLBACK_TYPE:
    """Set up MQTT topic subscriptions."""

    @callback
    def message_received(mqtt_message: mqtt.models.ReceiveMessage) -> None:
        """Handle new MQTT messages."""
        _LOGGER.debug(
            (
                "Message with timestamp %s, QOS %d, retain flagg %s, "
                "and payload length %d received "
                "from topic %s from subscription to topic %s"
            ),
            mqtt_message.timestamp,
            mqtt_message.qos,
            bool(mqtt_message.retain),
            len(mqtt_message.payload),
            mqtt_message.topic,
            mqtt_message.subscribed_topic,
        )
        meter_message = get_meter_message(mqtt_message)
        if meter_message:
            measure_queue.put_nowait(meter_message)

    topics = {x.strip() for x in config[CONF_MQTT_TOPICS].split(",")}

    _LOGGER.debug("Try to subscribe to %d MQTT topic(s): %s", len(topics), topics)
    unsubscibers = [
        await mqtt.client.async_subscribe(
            hass, topic, message_received, 1, encoding=None
        )
        for topic in topics
    ]
    _LOGGER.debug(
        "Successfully subscribed to %d MQTT topic(s): %s", len(topics), topics
    )

    @callback
    def unsubscribe_mqtt() -> None:
        _LOGGER.debug("Unsubscribe %d MQTT topic(s): %s", len(unsubscibers), topics)
        for unsubscribe in unsubscibers:
            unsubscribe()

    return unsubscribe_mqtt


def get_meter_message(
    mqtt_message: mqtt.models.ReceiveMessage,
) -> han_type.MeterMessageBase | None:
    """Get frame information part from mqtt message."""
    # Try first to read as HDLC-frame.

    # payload should always be bytes when encoding is None in async_subscribe
    payload: bytes = mqtt_message.payload  # type: ignore[attr-defined]
    message = _try_read_meter_message(payload)
    if message is not None:
        if message.message_type == han_type.MeterMessageType.P1:
            if message.is_valid:
                _LOGGER.debug(
                    "Got valid P1 message from topic %s: %s",
                    mqtt_message.topic,
                    payload.hex(),
                )
                return message

            _LOGGER.debug(
                "Got invalid P1 message from topic %s: %s",
                mqtt_message.topic,
                payload.hex(),
            )

            return None

        if message.is_valid:
            if message.payload is not None:
                _LOGGER.debug(
                    (
                        "Got valid frame of expected length with correct "
                        "checksum from topic %s: %s"
                    ),
                    mqtt_message.topic,
                    payload.hex(),
                )
                return message

            _LOGGER.debug(
                (
                    "Got empty frame of expected length with correct "
                    "checksum from topic %s: %s"
                ),
                mqtt_message.topic,
                payload.hex(),
            )

        _LOGGER.debug(
            "Got invalid frame from topic %s: %s",
            mqtt_message.topic,
            payload.hex(),
        )
        return None

    try:
        json_data = json.loads(mqtt_message.payload)
        if isinstance(json_data, dict):
            _LOGGER.debug(
                "Ignore JSON in payload without HDLC framing from topic %s: %s",
                mqtt_message.topic,
                json_data,
            )
            return None
    except ValueError:
        pass

    _LOGGER.debug(
        "Got payload without HDLC framing from topic %s: %s",
        mqtt_message.topic,
        payload.hex(),
    )

    # Try message containing DLMS (binary) message without HDLC framing
    # Some bridges encode the binary data as hex string, and this must be decoded
    if _is_hex_string(payload):
        payload = _hex_payload_to_binary(payload)
    return han_type.DlmsMessage(payload)


def _try_read_meter_message(payload: bytes) -> han_type.MeterMessageBase | None:
    """Try to parse HDLC-frame from payload."""
    if payload.startswith(b"/"):
        try:
            return dlde.DataReadout(payload)
        except ValueError as ex:
            _LOGGER.debug("Starts with '/', but not a valid P1 message: %s", ex)

    frame_reader = hdlc.HdlcFrameReader(
        use_octet_stuffing=False, use_abort_sequence=False
    )

    # Reader expects flag sequence in start and end.
    flag_seqeuence = hdlc.HdlcFrameReader.FLAG_SEQUENCE.to_bytes(1, byteorder="big")
    if not payload.startswith(flag_seqeuence):
        frame_reader.read(flag_seqeuence)

    frames = frame_reader.read(payload)
    if len(frames) == 0:
        # add flag sequence to the end
        frames = frame_reader.read(flag_seqeuence)

    if len(frames) > 0:
        return frames[0]

    if not _is_hex_string(payload):
        return None

    return _try_read_meter_message(_hex_payload_to_binary(payload))


def _is_hex_string(payload: bytes) -> bool:
    if (len(payload) % 2) == 0:
        try:
            int(payload, 16)
        except ValueError:
            return False
        else:
            return True
    return False


def _hex_payload_to_binary(payload: str | bytes) -> bytes:
    if isinstance(payload, bytes):
        return bytes.fromhex(payload.decode("utf8"))
    if isinstance(payload, str):
        return bytes.fromhex(payload)
    msg = f"Unsupported payload type: {type(payload)}"
    raise ValueError(msg)
