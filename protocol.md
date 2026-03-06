### Chat Protocol (client ↔ chat server)

**Frame structure**: `[length 4B big-endian][json_body N bytes]`

**JSON body**: `{"type": "MessageType", "data": {...}}`

Client → Server:
| Type | Data |
|------|------|
| `CreateRoom` | `{"my_username": str}` |
| `JoinRoom` | `{"room_code": str, "my_username": str}` |
| `LeaveRoom` | `{}` |
| `SendMessage` | `{"message": str}` |
| `SendFile` | `{"filename": str, "filedata": str (base64)}` |
| `GetStats` | `{}` |

Server → Client:
| Type | Data |
|------|------|
| `RoomCreated` | `{"room_code": str, "users": [str, ...]}` |
| `RoomJoined` | `{"room_code": str, "users": [str, ...]}` — full member list including the joining user |
| `RoomLeft` | `{"room_code": str}` |
| `IncomingMessage` | `{"from_username": str, "message": str}` |
| `IncomingFile` | `{"from_username": str, "filename": str, "filedata": str}` |
| `UserJoined` | `{"username": str, "room_code": str}` |
| `UserLeft` | `{"username": str, "room_code": str}` |
| `Stats` | `{"total_messages": int, "total_files": int, "total_users": int, "total_rooms": int}` |
| `Error` | `{"error_message": str}` |