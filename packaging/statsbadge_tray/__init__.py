"""The packaged app is this shim, and statsbadge itself goes in as a wheel.

Briefcase copies `sources` in as plain files, which arrive with no `.dist-info` at all.
statsbadge finds its extensions through entry points and reports its version from
installed metadata, so it has to be installed and not copied. It is named in `requires`
instead, and this is what the bundle starts.
"""
