"""Shared document layout constants per GB/T 9704—2012.

These constants are used by both the parser (calibration) and builder
(conversion, paragraph construction) subsystems.
"""

# Standard page margins (mm) per GB/T 9704-2012
STANDARD_MARGINS: dict[str, float] = {
    "top": 37.0,
    "bottom": 35.0,
    "left": 28.0,
    "right": 26.0,
}

# Standard line spacing (pt)
STANDARD_LINE_SPACING: float = 28.95

# Standard first-line indent (pt) = 2 * 16pt body text size
STANDARD_FIRST_INDENT: float = 32.0
