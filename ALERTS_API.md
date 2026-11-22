# Human-body notification alerts (preview)

The Vector/libxposed worker hooks `com.google.firebase.messaging.FirebaseMessagingService.handleIntent(android.content.Intent)` inside `com.cpplusworld.cpiotkam`. It captures only human-body detection messages identified by the observed `alert_msg` `hd` marker or the notification body text. The original Firebase method is always continued.

## Endpoint

`GET /v1/alerts?sinceId=0&limit=50`

Returns a bounded, in-memory queue of up to 200 alert events:

- `id`: monotonically increasing event ID within the current EzyLiv process
- `type`: `human_body_detected`
- `cameraId`, `title`, `body`, `alertMsg`, `messageId`
- `sentTimeMs`, `receivedAtMs`, `receivedElapsedRealtimeMs`
- `timestamp1Sec`, `timestamp2Sec`: the two numeric timestamp fields parsed from the observed `alert_msg` format; their semantic meanings are intentionally not inferred
- `timezoneEncoded`: the timezone field as present in the raw payload
- `latestId`, `oldestId`, `droppedThroughId`: queue/cursor diagnostics

`sinceId` is exclusive. Clients should retain the last processed `id` and pass it as `sinceId` on the next request. `limit` is clamped to 1..200.

## Delivery semantics and limitations

- This captures FCM payloads inside the EzyLiv process, not camera-side detection itself. It cannot report a detection for which no FCM message reaches the app.
- The event queue is in-memory and resets when the EzyLiv process restarts. The queue drops its oldest event when full; clients should inspect `droppedThroughId`.
- The hook is observational: it records the payload before continuing the original Firebase handler and does not suppress or modify EzyLiv notifications.
- Events can be captured when Android notification permission is denied, because payload reception and notification display are separate stages.
- The API is exposed through the existing CamXposed gateway and inherits its current network-access policy. The current bridge build can expose unauthenticated plaintext LAN HTTP when LAN access is enabled; use only on a trusted network and do not port-forward it.

## MCP tools

The accompanying MCP server adds `camera_alerts` and `camera_wait_for_human_alert`. The latter polls the queue until an event arrives or its timeout expires. These are request/response tools, not unsolicited MCP protocol notifications.


## Notification images

Alert events include `imageAvailable` and `imagePath`. When an image is available, call `camera_alert_image(alert_id)` to retrieve it as a native MCP image. The bridge endpoint is `GET /v1/alerts/{id}/image`. Image capture occurs when the app posts the notification; the bridge saves the full big-picture bitmap where available and uses the large icon only as a fallback.
