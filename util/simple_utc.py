
import datetime

# used for timezone conversion stuff; see https://stackoverflow.com/questions/19654578/python-utc-datetime-objects-iso-format-doesnt-include-z-zulu-or-zero-offset
class simple_utc(datetime.tzinfo):
    def tzname(self, **kwargs):
        return "UTC"
    def utcoffset(self, dt):
        return datetime.timedelta(0)
