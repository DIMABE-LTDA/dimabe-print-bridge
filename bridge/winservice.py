"""Servicio de Windows «Dimabe Print Bridge».

El instalador lo registra con `sc create ... binPath= "<exe> --service"`. Corre
el núcleo del puente (API, panel, cola, monitor, actualizador y vigilante) sin
la bandeja; si el proceso termina con error, Windows lo reinicia (acciones de
recuperación configuradas por el instalador).
"""
from __future__ import annotations

import logging

import servicemanager
import win32event
import win32service
import win32serviceutil

SERVICE_NAME = "DimabePrintBridge"
logger = logging.getLogger(__name__)


class DimabePrintBridgeService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = "Dimabe Print Bridge"
    _svc_description_ = "Puente local entre el POS de Odoo y las impresoras de esta caja."

    def __init__(self, args):
        super().__init__(args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._core = None

    def SvcStop(self):  # noqa: N802 - nombre impuesto por pywin32
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self._stop_event)

    def SvcDoRun(self):  # noqa: N802
        from main import start_core  # noqa: PLC0415 - evita importar el núcleo al registrar

        servicemanager.LogInfoMsg("Dimabe Print Bridge: iniciando")
        self._core = start_core(mode="service")
        win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)
        self._core.shutdown()
        servicemanager.LogInfoMsg("Dimabe Print Bridge: detenido")


def run_service_dispatcher() -> None:
    servicemanager.Initialize(SERVICE_NAME, None)
    servicemanager.PrepareToHostSingle(DimabePrintBridgeService)
    servicemanager.StartServiceCtrlDispatcher()
