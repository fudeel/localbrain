# Local AI Brain: Client Guide

## In short

Local AI Brain (Localbrain) runs AI agents on your own hardware and gives every client the same HTTP API. The brain keeps agents available and stores readings and conversations. Clients bring the sensors, rules and actuators, and they decide what to do with each answer.

Two rules shape everything below:

- **An agent's answer is advice, not a command.** The client checks it before anything real happens.
- **Services adapt to the brain.** A new client uses the public API, and the brain doesn't change to suit it.

| Term | Meaning | Example |
| --- | --- | --- |
| Agent | A model the brain can install and load into memory | `qwen3:4b-q4_K_M` |
| Client | Any program that calls the API | A sensor script, a game, a house controller |
| Device | A named entry created when a client sends a reading | `kitchen` |
| Reading | One measured value from a device | `temperature` = 21.5 °C |

## What the brain does

The brain runs agents and keeps shared data, but it never acts on the world by itself. Anything that switches, moves or alerts has to be a client.

| Area | The brain does | The brain does not |
| --- | --- | --- |
| Agents | Installs and loads them, and reloads them after a restart | Load one unless asked: a client must send `auto_mount` |
| Answers | Passes each request to the model and returns the answer, whole or streamed | Check that the answer is correct |
| Tools | Returns the agent's function calls to the client | Run those functions |
| Readings | Stores them for 30 days by default and publishes an event for each report | Act on them: it has no rules, alerts or schedules |
| Devices | Lists each one with its latest readings | Send commands to devices or connect out to them |
| History | Keeps a conversation under a `conversation_id` | Remember anything between requests without one |
| Access | Requires a token on a network and blocks foreign `Host` and `Origin` headers | Tell clients apart or encrypt traffic |
| Local only | Refuses cloud models and runs agents on this machine | Work without internet for first-time downloads of Ollama or an agent |

## What the brain expects from clients

The brain rejects a request that breaks these rules and says why, so a mistake shows up at once instead of being silently ignored.

| Rule | Details |
| --- | --- |
| Address | Every API path starts with `/api/v1`. The default port is 8484. |
| Format | JSON bodies with a `Content-Length` header. Chunked uploads are refused. |
| Exact fields | Unknown fields are rejected, so a typo returns an error. |
| Token | Send `Authorization: Bearer <token>` when a token is required, which is automatic on a network. Only `GET /health` and the dashboard's own page files are public. |
| Device IDs | Letters, numbers and `. _ : -`, starting with a letter or number, up to 128 characters. |
| Readings | Each needs a `metric` and a `value`, which is a number, text or true/false. `unit` is optional. |
| Report size | Up to 200 readings in one report. |
| Request size | Up to 32 MiB, images included. This is configurable. |
| Chat | Send either `message` or `messages`, never both. |
| Agent state | The agent must be installed and mounted, or the request must send `auto_mount: true`. |
| Web pages | A page from another site is refused unless its origin is listed in `allowed_origins`. Programs that aren't browsers aren't affected. |

The brain records when each reading arrives, and a client can't send its own timestamp. A reading saved offline and sent later is stored with the later time.

## What clients should do

A client needs only HTTP, and most clients need just the first three calls below. Python programs can use [`brain/sdk.py`](../brain/sdk.py), which wraps the same API.

| Goal | Call | Notes |
| --- | --- | --- |
| Check the brain is up | `GET /health` | Public, so it needs no token |
| Report readings | `POST /devices/{id}/readings` | Creates the device on first use, which lists it under Connections |
| Ask an agent | `POST /agents/{name}/chat` | Set `system`, `format` and `think` for each request |
| See devices and history | `GET /devices`, `GET /devices/{id}/readings` | History returns 200 readings by default, up to 5,000 |
| React as things happen | `GET /events` | A live stream of events, including `device.reading` |
| Remove a retired device | `DELETE /devices/{id}` | Also deletes its history |

This request asks for an answer a program can read directly:

```bash
curl http://127.0.0.1:8484/api/v1/agents/qwen3:4b-q4_K_M/chat \
  -d '{"message": "The room is 29 C. Should the fan run?", "think": false, "format": {"type": "object", "properties": {"fan": {"type": "boolean"}}, "required": ["fan"]}}'
```

The answer is in the reply's `content` field as JSON text, such as `{"fan": true}`. The client then checks that value and decides what to do.

## What clients can expect back

Clients can rely on the shape of each reply, but not on an agent being right or fast. Every error has the same form: `{"error": {"code": "...", "message": "...", "hint": "..."}}`, and `hint` appears only when the brain has a suggestion.

| Status | Meaning | What the client should do |
| --- | --- | --- |
| 200, 201 | Accepted. 201 is returned for a reading. | Continue |
| 400 | The request is invalid | Fix it. Retrying won't help. |
| 401 | The token is missing or wrong | Check the token |
| 403 | Foreign `Host` or `Origin`, or a cloud agent | Fix the setting or choose a local agent |
| 404 | No such agent, device or conversation | Check the name |
| 409 | The agent isn't installed or mounted, doesn't fit this machine, or can't chat | Mount it or choose another agent |
| 413 | The request is too large | Send less |
| 502, 503 | Ollama failed or can't be reached | Retry later, waiting longer each time |

**A streamed chat can fail after a 200 status.** Once streaming has begun, a failure arrives as an `error` event in the stream, so a client must check for it.

| Topic | What to expect |
| --- | --- |
| Answer shape | `format` is enforced, so the answer fits the schema. `system` is only a request, which the agent may not follow. |
| Answer quality | A valid answer can still be wrong, and the same question can get different answers. |
| Speed | On the 8-core, CPU-only test machine, replies ran at about 3.5 tokens per second. A one-word JSON decision took about 3 seconds, and a Robot Mower move took about 5 to 6. |
| Loading | A request using `auto_mount` also waits for the agent to load into memory. |
| Memory | Each request stands alone unless it sends a `conversation_id` or its own history. |
| Sensor context | `sensor_context` gives the agent the latest readings from every device. Text is flattened and cut to 80 characters. |
| Events | There is no replay, so a client misses events while disconnected. It can catch up with `GET /devices`. A heartbeat is sent every 15 seconds when idle. |
| Slow listeners | Each event listener has a backlog of 256 events, and the oldest are dropped first. |
| Stored data | Readings are deleted after 30 days by default. |
| `Live` status | It means a report arrived in the last 5 minutes. The brain doesn't track whether a client is connected. |

## Best practices for every client

The most useful habit is to give the agent small decisions. In the Robot Mower tests, an agent given raw map coordinates cut 0 of 7 grass tiles in both games. When the service worked out the options first, it cut 7 of 7 and then 10 of 10.

- **Do the calculating in your code.** Work out the facts, then let the agent choose between clear options.
- **Require a format.** If code reads the answer, use `format` with a JSON schema, and limit values with `enum`.
- **Check every answer.** Confirm the value is allowed and makes sense before acting on it.
- **Plan for failure.** Decide what happens when the agent is wrong, slow or unavailable.
- **Keep answers short.** Send `think: false` unless you need reasoning, and limit length with `options.num_predict`.
- **Set your own timeout.** The Python client waits up to 30 minutes by default, so choose a limit that suits your task.
- **Retry only what can recover.** Retry a 502 or 503 with growing delays. Don't retry other errors unchanged.
- **Mount agents on purpose.** Use `auto_mount` only in clients trusted to claim that memory.
- **Send only the history you need.** Use `conversation_id` when context matters, because longer prompts take longer to process.
- **Share the machine politely.** All clients use the same hardware, so heavy use by one can slow the others.
- **Keep settings outside the code.** Make the brain's address and the agent's name configurable.
- **Protect the token.** Keep it in `BRAIN_TOKEN` or another local secret store, and never put it in code, a repo or a chat.

## Best practices for IoT

Split the work into three jobs: sensors report, a controller decides, and actuators act. The brain can't send commands to devices and agents take seconds to answer, so your controller is responsible for safety.

![A flow diagram. A sensor sends a reading to the brain, which sends an event to your controller. The controller asks the agent through the brain and gets an answer, then checks whether it is allowed and still recent. If yes, the actuator carries out the action. If no, a safe fallback applies. A critical value skips this path and goes through local safety logic straight to the actuator.](images/iot-flow.png)

Critical responses skip the agent entirely, shown by the dashed path. Everything else has to pass the controller's check before it reaches an actuator.

### Match the agent's role to the risk

This table is a recommended starting policy, not something the brain enforces.

| Risk | Examples | What an agent may do |
| --- | --- | --- |
| Low | Lamps, announcements, games | Trigger an action after your code checks it is allowed |
| Medium | Thermostats, blinds, watering | Suggest a value inside fixed limits your code enforces |
| High | Locks, ovens, gas, alarms, mains power | Inform a person. It never triggers the action. |

### Sensors

- **Report at the pace the value changes.** Send temperature every 30 to 60 seconds, and send door or motion changes as they happen.
- **Count the storage.** One metric every 30 seconds adds 86,400 rows over 30 days, which matters on a Raspberry Pi's SD card.
- **Group readings.** Send all of a device's metrics in one report.
- **Use stable, unique IDs,** such as `kitchen-climate-1`. Two devices sharing an ID become one entry.
- **Keep secrets out of `meta`.** Anyone with API access can read it.
- **Delete retired devices.** Otherwise their last readings still reach any agent using `sensor_context`.

The brain treats `temp` and `temperature` as different metrics, so use one set of names everywhere. This is our recommended list:

| Metric | Value | Unit |
| --- | --- | --- |
| `temperature` | Number | °C |
| `humidity` | Number | % |
| `power` | Number | W |
| `battery` | Number | % |
| `motion` | true or false | None |
| `door_open` | true or false | None |

### Controllers

- **Keep agents out of time-critical paths.** Smoke, leak and overheating responses belong in logic on or near the device.
- **Check a reading's age.** Each stored reading has an `at` time, so don't act on old data.
- **Send only the readings a decision needs.** Put them in `message`, because `sensor_context` includes every device and grows with your network.
- **Enforce limits in code.** A heating setpoint, for example, should always be clamped to a range you chose.
- **Limit how often actions run.** A wrong answer then can't keep switching a relay on and off.
- **Log each decision.** Record the reading, the answer and the action, so you can see why something happened.
- **Have a fallback for when the brain is down.** Use a fixed rule or a safe state, never "wait for the agent".

### Small and battery-powered devices

- **Only send readings.** A device needs just an HTTP request, so leave agent calls to the controller.
- **Send the current value after reconnecting.** A saved backlog would be stored with its arrival time, not the time it was measured.
- **Expect idle connections to close.** The brain closes them after 75 seconds, so a new connection per report is fine.

### Network and security

- **Never expose port 8484 to the internet.** Keep the brain on a private network, ideally with IoT devices on their own segment.
- **Treat the token as full access.** It can also install and delete agents, so low-trust devices should report through a gateway that holds it.
- **Remember traffic is plain HTTP.** Anyone watching the network can read it, token included, so use a trusted network or a TLS reverse proxy.
- **Treat device text as data.** Flattening reduces prompt injection but doesn't eliminate it, which is another reason to check every answer.

## Known limits today

The biggest gap for IoT is that one shared token gives every client full access, and that nothing has yet been tested on real IoT hardware.

| Limit | What it means for a client |
| --- | --- |
| One shared token | Clients can't be told apart or given different permissions, and any of them can overwrite another's device entry |
| No commands to devices | A controller has to reach its own actuators directly |
| No rules or alerts | A reading only gets stored and published, so a client must watch for what matters |
| Metric names aren't checked | The recommended list above is a convention that nothing enforces |
| No client timestamps | Offline readings can't keep the time they were measured |
| No event replay | A client that reconnects has to catch up from `GET /devices` |
| Plain HTTP | Encryption needs a separate reverse proxy |

These haven't been tested:

- Raspberry Pi and other ARM boards, Jetson, and NVIDIA or AMD GPUs
- Microcontrollers talking to the brain, such as an ESP32
- Many devices reporting at once
- Real container limits, and the Docker, Compose, Kubernetes and systemd files

All testing so far ran on one Ubuntu x86-64 virtual machine with no GPU and 8 GB of RAM, so the speeds in this guide are specific to it. The Room Advisor example also doesn't handle a streamed `error` event yet, so it reports an empty answer instead.

## Checklist for a new client

- [ ] It uses only the public API or `brain/sdk.py`, with no changes to the brain
- [ ] The brain's address and the agent's name are settings
- [ ] The token comes from `BRAIN_TOKEN` or a local secret store
- [ ] Its device ID is unique, and its metrics follow the recommended names and units
- [ ] It reports readings no faster than the value changes
- [ ] It requires a JSON format wherever code reads the answer
- [ ] It checks every answer before acting
- [ ] It handles errors, timeouts and streamed `error` events
- [ ] It has a safe fallback for when the brain or agent is unavailable
- [ ] Its agent's role matches the risk of what it controls
- [ ] Critical safety responses work without an agent
- [ ] It logs each decision and limits how often actions run
