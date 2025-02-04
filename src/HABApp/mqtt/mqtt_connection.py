import logging
import typing
from pathlib import Path

import paho.mqtt.client as mqtt

import HABApp
from HABApp.core import Items
from HABApp.core.wrapper import log_exception
from HABApp.mqtt.events import MqttValueChangeEvent, MqttValueUpdateEvent
from HABApp.mqtt.mqtt_payload import get_msg_payload
from HABApp.runtime import shutdown

log = logging.getLogger('HABApp.mqtt.connection')
log_msg = logging.getLogger('HABApp.EventBus.mqtt')


class MqttStatus:
    def __init__(self):
        self.loop_started = False
        self.connected = False
        self.client: typing.Optional[mqtt.Client] = None
        self.subscriptions: typing.List[typing.Tuple[str, int]] = []


STATUS = MqttStatus()


def setup():
    config = HABApp.config.CONFIG.mqtt

    # config changes
    config.subscribe.subscribe_for_changes(subscription_changed)
    config.connection.subscribe_for_changes(connect)

    # shutdown
    shutdown.register_func(disconnect, msg='Disconnecting MQTT')


def connect():
    config = HABApp.config.CONFIG.mqtt

    if not config.connection.host:
        log.info('MQTT disabled')
        disconnect()
        return None

    if STATUS.connected:
        log.info('disconnecting')
        STATUS.client.disconnect()
        STATUS.connected = False

    STATUS.client = mqtt_client = mqtt.Client(
        client_id=config.connection.client_id,
        clean_session=False
    )

    if config.connection.tls:
        # add option to specify tls certificate
        ca_cert = config.connection.tls_ca_cert
        if ca_cert != "":
            if not Path(ca_cert).is_file():
                log.error(f'Ca cert file does not exist: {ca_cert}')
                # don't connect without the properly set certificate
                disconnect()
                return None
            else:
                log.debug(f"CA cert path: {ca_cert}")
                mqtt_client.tls_set(ca_cert)
        else:
            mqtt_client.tls_set()

        # we can only set tls_insecure if we have a tls connection
        if config.connection.tls_insecure:
            log.warning('Verification of server hostname in server certificate disabled!')
            log.warning('Use this only for testing, not for a real system!')
            mqtt_client.tls_insecure_set(True)

    # set user/pw if required
    user = config.connection.user
    pw = config.connection.password
    if user:
        mqtt_client.username_pw_set(user, pw if pw else None)

    # setup callbacks
    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_message = process_msg

    mqtt_client.connect_async(
        config.connection.host, port=config.connection.port, keepalive=60
    )

    log.info(f'Connecting to {config.connection.host}:{config.connection.port}')

    if not STATUS.loop_started:
        mqtt_client.loop_start()
    STATUS.loop_started = True


@log_exception
def disconnect():
    if STATUS.connected:
        STATUS.client.disconnect()
        STATUS.connected = False
    if STATUS.loop_started:
        STATUS.client.loop_stop()
        STATUS.loop_started = False

    STATUS.client = None


@log_exception
def on_connect(client, userdata, flags, rc):
    log.log(logging.INFO if not rc else logging.ERROR, mqtt.connack_string(rc))
    if rc:
        return None
    STATUS.connected = True

    STATUS.subscriptions.clear()
    subscription_changed()


@log_exception
def on_disconnect(client, userdata, rc):
    log.log(logging.INFO if not rc else logging.ERROR, f'Disconnect: {mqtt.error_string(rc)} ({rc})')
    STATUS.connected = False


@log_exception
def subscription_changed():
    if not STATUS.connected:
        return None

    if STATUS.subscriptions:
        unsubscribe = [k[0] for k in STATUS.subscriptions]
        log.debug('Unsubscribing from:')
        for t in unsubscribe:
            log.debug(f' - "{t}"')
        STATUS.client.unsubscribe(unsubscribe)

    topics = HABApp.config.CONFIG.mqtt.subscribe.topics
    default_qos = HABApp.config.CONFIG.mqtt.subscribe.qos
    STATUS.subscriptions = [(topic, qos if qos is not None else default_qos) for topic, qos in topics]
    log.debug('Subscribing to:')
    for topic, qos in STATUS.subscriptions:
        log.debug(f' - "{topic}" (QoS {qos:d})')
    STATUS.client.subscribe(STATUS.subscriptions)


@log_exception
def process_msg(client, userdata, message: mqtt.MQTTMessage):

    topic, payload = get_msg_payload(message)
    if topic is None:
        return None

    _item = None    # type: typing.Optional[HABApp.mqtt.items.MqttBaseItem]
    try:
        _item = Items.get_item(topic)   # type: HABApp.mqtt.items.MqttBaseItem
    except HABApp.core.Items.ItemNotFoundException:
        # only create items for if the message has the retain flag
        if message.retain:
            _item = Items.create_item(topic, HABApp.mqtt.items.MqttItem)  # type: HABApp.mqtt.items.MqttItem

    # we don't have an item -> we process only the event
    if _item is None:
        HABApp.core.EventBus.post_event(topic, MqttValueUpdateEvent(topic, payload))
        return None

    # Remember state and update item before doing callbacks
    _old_state = _item.value
    _item.set_value(payload)

    # Post events
    HABApp.core.EventBus.post_event(topic, MqttValueUpdateEvent(topic, payload))
    if _old_state != payload:
        HABApp.core.EventBus.post_event(topic, MqttValueChangeEvent(topic, payload, _old_state))
