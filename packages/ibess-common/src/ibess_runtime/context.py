"""Live view of a composition root; compatibility entry points remain injectable."""


class Namespace:
    def __init__(self, values):
        object.__setattr__(self, "_values", values)

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        self._values[name] = value
