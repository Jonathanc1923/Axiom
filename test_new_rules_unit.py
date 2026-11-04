import unittest
import pandas as pd
import numpy as np
import time
from unittest.mock import MagicMock
from engine import AxiomEngine
import config

class TestNewVwap30mRules(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.account_id = 29324973
        self.mock_client.get_open_positions.return_value = []
        self.mock_client.search_open_orders.return_value = []
        self.mock_client.resolve_contract.return_value = {"id": "CON_ES_TEST", "name": "ESZ6"}
        self.engine = AxiomEngine(client=self.mock_client, default_symbol="ES")
        self.engine.account_id = 29324973

    def _create_single_bar_df(self, bar, vwap=5000.0, std_dev=20.0):
        data = [
            {
                "Timestamp": pd.Timestamp("2026-11-04 04:00:00", tz="UTC"),
                "Open": bar["o"], "High": bar["h"], "Low": bar["l"], "Close": bar["c"],
                "Volume": 1000, "VWAP": vwap, "Upper_Band": vwap + std_dev, "Lower_Band": vwap - std_dev
            }
        ]
        return pd.DataFrame(data)

    def test_setup_short_valid(self):
        """Vela actual de 30m: abre bajo VWAP, mecha toca o pasa VWAP, precio actual bajo VWAP."""
        vwap = 5000.0
        std_dev = 20.0 # 50% = 10.0 pts (40 ticks en ES)
        # Bar: opens 4995, wick tests 5002 >= 5000, current price is 4996 < 5000
        bar = {"o": 4995.0, "h": 5002.0, "l": 4990.0, "c": 4996.0}

        df = self._create_single_bar_df(bar, vwap=vwap, std_dev=std_dev)
        setup = self.engine.verificar_setup_vwap(df, "ES", current_price=4996.0)

        self.assertIsNotNone(setup, "Debe generar setup SHORT")
        self.assertEqual(setup["direccion"], "SHORT")
        # SL fijo a 5 ticks de VWAP (5 * 0.25 = 1.25 pts arriba de VWAP = 5001.25)
        self.assertEqual(setup["precio_sl"], 5001.25, "SL debe estar fijo a 5 ticks por encima del VWAP (5001.25)")
        # TP con el 50% de la distancia entre VWAP y siguiente banda (10.0 pts) desde la apertura de operacion
        # 4996.0 - 10.0 = 4986.0 (gana exactamente 10 pts = 40 ticks)
        self.assertEqual(setup["precio_tp"], 4986.0, "TP debe estar a 10.0 pts de la entrada (4986.0)")
        self.assertEqual(setup["tp_ticks"], 40, "TP debe ganar exactamente 40 ticks")

    def test_setup_long_valid(self):
        """Vela actual de 30m: abre sobre VWAP, mecha toca o pasa VWAP, precio actual sobre VWAP."""
        vwap = 5000.0
        std_dev = 20.0 # 50% = 10.0 pts (40 ticks en ES)
        # Bar: opens 5005, wick tests 4998 <= 5000, current price is 5004 > 5000
        bar = {"o": 5005.0, "h": 5010.0, "l": 4998.0, "c": 5004.0}

        df = self._create_single_bar_df(bar, vwap=vwap, std_dev=std_dev)
        setup = self.engine.verificar_setup_vwap(df, "ES", current_price=5004.0)

        self.assertIsNotNone(setup, "Debe generar setup LONG")
        self.assertEqual(setup["direccion"], "LONG")
        # SL fijo a 5 ticks de VWAP (5 * 0.25 = 1.25 pts abajo de VWAP = 4998.75)
        self.assertEqual(setup["precio_sl"], 4998.75, "SL debe estar fijo a 5 ticks por debajo del VWAP (4998.75)")
        # TP con el 50% de la distancia entre VWAP y siguiente banda (10.0 pts) desde la apertura de operacion
        # 5004.0 + 10.0 = 5014.0 (gana exactamente 10 pts = 40 ticks)
        self.assertEqual(setup["precio_tp"], 5014.0, "TP debe estar a 10.0 pts de la entrada (5014.0)")
        self.assertEqual(setup["tp_ticks"], 40, "TP debe ganar exactamente 40 ticks")

    def test_setup_rejected_if_no_wick_touched_vwap(self):
        """Rechazar si la mecha no alcanzó la línea del VWAP."""
        vwap = 5000.0
        # High 4998 < 5000 (no tocó VWAP)
        bar = {"o": 4990.0, "h": 4998.0, "l": 4985.0, "c": 4995.0}
        df = self._create_single_bar_df(bar, vwap=vwap, std_dev=20.0)
        setup = self.engine.verificar_setup_vwap(df, "ES", current_price=4995.0)
        self.assertIsNone(setup, "No debe entrar si la mecha no tocó el VWAP")

    def test_setup_rejected_if_price_closed_above_vwap_for_short(self):
        """Rechazar si la vela rompió al alza en lugar de rebotar hacia abajo."""
        vwap = 5000.0
        # Precio actual 5002 > 5000 (rompió VWAP)
        bar = {"o": 4995.0, "h": 5005.0, "l": 4992.0, "c": 5002.0}
        df = self._create_single_bar_df(bar, vwap=vwap, std_dev=20.0)
        setup = self.engine.verificar_setup_vwap(df, "ES", current_price=5002.0)
        self.assertIsNone(setup, "No debe entrar SHORT si el precio está por encima del VWAP")

    def test_sl_remains_fixed_no_dynamic_trailing(self):
        """Verifica que el SL permanezca fijo y no se mueva con USE_DYNAMIC_SL=False."""
        sym = "ES"
        entry_price = 4996.0
        vwap = 5000.0
        sl_fijo = 5001.25 # 5 ticks de VWAP
        tp_fijo = 4986.0
        
        self.engine.symbol_states[sym]["active_trade"] = {
            "symbol": sym,
            "contract_id": "CON_ES_TEST",
            "direction": "SHORT",
            "entry_price": entry_price,
            "vwap_cruce": vwap,
            "entry_timestamp": time.time(),
            "initial_sl_price": sl_fijo,
            "current_sl_price": sl_fijo,
            "initial_tp_price": tp_fijo,
            "current_tp_price": tp_fijo,
            "base_tp_pts": 10.0,
            "d_tp_ini": 10.0,
            "be_plus_2_active": False,
            "sl_stage": 0.0,
            "highest_price": entry_price,
            "lowest_price": entry_price
        }
        self.engine.symbol_states[sym]["has_open_position"] = True

        self.mock_client.get_open_positions.return_value = [{"contractId": "CON_ES_TEST", "size": -1}]
        self.mock_client.search_open_orders.return_value = [
            {"id": 102, "contractId": "CON_ES_TEST", "type": 4, "stopPrice": sl_fijo, "side": 0}
        ]

        # Precio avanza 70% hacia TP (precio a 4989.0): con USE_DYNAMIC_SL=False NO debe mover el SL
        self.engine.monitorear_posicion_abierta(sym, current_price=4989.0, cached_positions=[{"contractId": "CON_ES_TEST", "size": -1}])
        trade = self.engine.symbol_states[sym]["active_trade"]
        self.assertFalse(trade.get("be_plus_2_active", False), "No debe activar BE dinámico")
        self.assertEqual(trade["current_sl_price"], sl_fijo, "El SL debe permanecer 100% fijo")
        self.mock_client.modify_order.assert_not_called()

    def test_new_configuration_values(self):
        """Verifica que los parámetros de configuración coincidan con las nuevas reglas del usuario."""
        self.assertEqual(config.ACCOUNT_NAME, "PRAC-V2-460183-95785678")
        self.assertEqual(config.BAR_TIMEFRAME_MINUTES, 30)
        self.assertEqual(config.SL_VWAP_DISTANCE_TICKS, 5)
        self.assertEqual(config.TP_VWAP_BAND_RATIO, 0.50)
        self.assertFalse(config.USE_DYNAMIC_SL)
        self.assertEqual(config.POLL_INTERVAL_SECONDS, 1.0)

if __name__ == "__main__":
    unittest.main()
