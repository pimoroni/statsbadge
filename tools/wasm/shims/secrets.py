"""The firmware's `secrets`, which the badge writes at install and this port has not.

Empty on purpose: app.py reads WIFI_SSID with a default, and finding none is the path
that draws "No WiFi" rather than the one that tries to connect.
"""
