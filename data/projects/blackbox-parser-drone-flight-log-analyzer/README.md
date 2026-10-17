# BlackBox-Parser

A modular command-line tool for parsing and analyzing flight logs.
It fully supports both:
- MAVLink telemetry logs (`.tlog`)
- ArduPilot DataFlash binary logs (`.bin`)

## Features
- **Format Auto-Detection**: Automatically parses `.tlog` and `.bin` logs into a unified internal data schema.
- **Summary**: Generates an overview of flight duration, speed, max altitude, battery usage, cumulative distance, displacement, climb/descent rates, and a chronological flight events timeline.
- **CSV Export**: Extracts telemetry into `output/csv/` for GPS, Attitude, Altitude, and Battery data.
- **Graphs**: Generates PNG visualizations in `output/graphs/` for Altitude, Battery, Speed, Power, and Attitude vs. Time.
- **PDF Report**: Generates a self-contained PDF report with summary, embedded graphs, and warnings in `output/flight_report.pdf`.
- **Flight Track Map**: Generates an interactive HTML map with color-coded flight path (green=slow, red=fast).
- **Anomalies**: Detects critical warnings (low battery, altitude drop, excessive roll/pitch angles).
- **Batch Mode**: Analyze multiple logs at once and compare them in a side-by-side table.
- **Interactive Mode**: Menu-driven interface for users who prefer not to memorize CLI flags.

## Installation

```bash
# Clone the repository
git clone https://github.com/Punit-154/BlackBox-Parser.git
cd BlackBox-Parser

# Create a virtual environment (optional but recommended)
python -m venv venv

# Activate the virtual environment
venv\Scripts\activate  # On Windows
# source venv/bin/activate  # On macOS/Linux

# Install requirements
pip install -r requirements.txt
```

## Usage

```bash
# View help
python analyzer.py --help

# Run specific analyses on a .tlog or .bin file
python analyzer.py logs/sample.tlog --summary
python analyzer.py logs/sample.bin --export --graphs

# Run all analysis steps at once (summary, export, graphs, warnings)
python analyzer.py logs/sample.tlog --all

# Generate a self-contained PDF report
python analyzer.py logs/sample.tlog --report

# Generate an interactive flight track map
python analyzer.py logs/sample.tlog --map

# Batch analyze all logs in a folder
python analyzer.py --batch logs/

# Interactive mode — launch menu, then provide log file when prompted
python analyzer.py --interactive

# Interactive mode — skip the file prompt by passing the path upfront
python analyzer.py logs/sample.tlog --interactive
```

## Architecture Notes
The project is built on a "schema normalization" approach:
- `parser.py` maps the different raw messages from `.tlog` (`GPS_RAW_INT`, `ATTITUDE`, etc.) and `.bin` (`GPS`, `ATT`, etc.) into a **common internal data structure** using human-readable units (meters, degrees, m/s, volts).
- Missing data fields in `.bin` files (such as `battery_remaining`, `vx`, `vy`, `vz`, `relative_alt`) are safely filtered out, handled as `None`, or skipped in the downstream analyzer modules (`summary.py`, `graphs.py`, `anomalies.py`, `exporter.py`) to prevent errors and ensure backward compatibility.
- **Interactive mode** (`analyzer.py`) runs a `while True` event loop that dispatches user menu selections to the same standalone functions used by the flag-based CLI. The `logfile` argument is made optional (`nargs="?"`) so the program can prompt for a file path at runtime. A "Change Log File" option allows swapping the loaded log without restarting.

## Understanding the Metrics & Technical Background

### Cumulative Flight Distance & Max Displacement
- **What it is**: 
  - **Cumulative Distance**: The total ground distance traveled by the drone over the entire flight.
  - **Max Displacement**: The furthest straight-line distance the drone flew away from its original takeoff point.
- **Why it exists**: Useful for verifying that a flight stayed within required geofence limits (Displacement) and estimating total motor/propulsion work done (Distance).
- **Technical Background**: Calculated using the **Haversine formula**. Because the Earth is a sphere, we cannot use simple 2D Cartesian math (`sqrt(x^2 + y^2)`) for GPS coordinates. The Haversine formula accounts for the curvature of the Earth to accurately measure the great-circle distance between two (Latitude, Longitude) points. 

### Peak Climb & Descent Rates
- **What it is**: The maximum vertical velocity (in m/s) at which the drone gained or lost altitude.
- **Why it exists**: Critical for safety analysis. Rapid descents can cause a drone to enter its own prop-wash (Vortex Ring State), leading to a crash.
- **Technical Background**: Calculated using the discrete derivative of altitude with respect to time (`dz / dt`). By comparing the `relative_alt` at two consecutive timestamps, we can compute the vertical speed.

### Flight Events Timeline
- **What it is**: A chronologically ordered timeline of significant flight events (e.g., "Armed", "Takeoff", "Mode AUTO") printed directly in the terminal summary.
- **Why it exists**: It provides critical context to understand the progression of the flight without needing to open external graphing tools. Knowing when the drone armed or changed flight modes is essential for diagnosing issues.
- **Technical Background**: MAVLink (`.tlog`) and DataFlash (`.bin`) logs contain distinct text strings and event IDs. The analyzer parses `STATUSTEXT`, `MSG`, `MODE`, and `EV` packets, filters for keywords like "arm", "takeoff", or "mode", and formats them into a human-readable chronological timeline.
