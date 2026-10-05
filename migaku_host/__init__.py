"""Host side of migaku-games: everything that has to run on the player's own machine (capture,
the overlay window, notifications, freezing). Stdlib only, with one backend per platform for
each job, picked automatically (see desktop.py) or set in the config's `host` section.
"""
