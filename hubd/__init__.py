"""
hubd - Audio Hub Controller Daemon

Single Python daemon that combines:
1. Ducking engine (event-driven via pulsectl)
2. MQTT/Home Assistant bridge
3. IR remote control (FLIRC)
4. Status publishing

Replaces multiple services from the old implementation.
"""

__version__ = "1.0.0"
