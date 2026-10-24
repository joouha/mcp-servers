# FMD MCP Server

MCP server for [FMD (Find My Device)](https://fmd-foss.org/) - view location history, request current location, and send commands to your device.

## Features

- **Location History**: Retrieve and decrypt recent location data from your FMD server
- **Current Location**: Get the most recent known location
- **Request Location**: Trigger a fresh GPS/cell/WiFi location fix on your device
- **Device Info**: View battery, connectivity, and device status
- **Pictures**: List recent picture metadata and take new pictures
- **Commands**: Play sound, lock device, toggle Bluetooth/Do Not Disturb, set ringer mode
- **Export**: Export all location and picture data to a ZIP file

## Installation

```bash
# From the workspace root
cd packages/fmd-mcp
pip install -e .
```

Or install with dev dependencies:

```bash
pip install -e ".[dev]"
```

## Configuration

Configure via environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FMD_URL` | No | `https://server.fmd-foss.org/` | Your FMD server URL |
| `FMD_USERNAME` | **Yes** | - | Your FMD username/email |
| `FMD_PASSWORD` | **Yes** | - | Your FMD password |
| `FMD_DEVICE_ID` | No | Username | Specific device ID to query |
| `FMD_TIMEOUT` | No | `30.0` | Request timeout in seconds |
| `FMD_VERIFY_TLS` | No | `true` | Verify TLS certificates (set to `false` for self-signed) |
| `FMD_LOG_LEVEL` | No | `WARNING` | Log level (DEBUG, INFO, WARNING, ERROR) |

## Usage

### As an MCP Server

Add to your MCP client configuration:

```json
{
  "mcpServers": {
    "fmd": {
      "command": "fmd-mcp",
      "env": {
        "FMD_URL": "https://your-fmd-server.example.com",
        "FMD_USERNAME": "your-username",
        "FMD_PASSWORD": "your-password",
        "FMD_DEVICE_ID": "optional-device-id"
      }
    }
  }
}
```

### Available Tools

| Tool | Description |
|------|-------------|
| `get_location_history` | Get recent location history (count: 1-100) |
| `get_current_location` | Get the most recent known location |
| `request_location` | Request a fresh location fix (source: gps, cell, wifi, all, last) |
| `get_device_location` | Get location via Device helper (alternative method) |
| `get_device_info` | Get device info (battery, connectivity, etc.) |
| `get_pictures` | Get recent picture metadata |
| `take_picture` | Take a picture (front/back camera) |
| `play_sound` | Play a sound on the device |
| `lock_device` | Lock the device with optional message |
| `set_bluetooth` | Enable/disable Bluetooth |
| `set_do_not_disturb` | Enable/disable Do Not Disturb |
| `set_ringer_mode` | Set ringer mode (normal/vibrate/silent) |
| `export_data` | Export all data to ZIP file |

### Available Resources

| Resource | Description |
|----------|-------------|
| `fmd://device` | Device information |
| `fmd://location/current` | Most recent location |
| `fmd://location/history/{count}` | Recent location history |
| `fmd://pictures/{count}` | Recent picture metadata |

## Example Usage

```python
# Get last 20 locations
locations = await get_location_history(count=20)

# Request a fresh GPS fix
await request_location(source="gps")

# Get current location after requesting
current = await get_current_location()

# Play sound to find phone
await play_sound()

# Lock device with message
await lock_device(message="Lost phone. Please call +1-555-1234")
```

## Development

Run tests:

```bash
pytest tests/
```

Run the server directly:

```bash
python -m fmd_mcp
```

## Security Notes

- The server uses `drop_password=True` - the raw password is not kept in memory after authentication
- TLS verification is enabled by default. For self-signed certificates, set `FMD_VERIFY_TLS=false` (not recommended for production)
- Consider using a custom CA bundle for self-signed certificates instead of disabling verification

## License

MIT