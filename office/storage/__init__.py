"""The office plugin's storage spine (S147-1).

``DocumentStore`` is the single home for read/write/delete and envelope
encryption over ``filesystem_manager.for_plugin("office")``. Nothing else in
the plugin touches the filespace.
"""
