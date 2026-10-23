class BridgeError(Exception):
    """Error que se devuelve al cliente con su código HTTP."""

    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.message = message
        self.status = status
