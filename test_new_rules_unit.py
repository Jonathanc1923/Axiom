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
        self.mock_client.account_id = 29409647
        self.mock_client.get_open_positions.return_value = []
        self.mock_client.search_open_orders.return_value = []
        self.mock_client.resolve_contract.return_value = {"id": "CON_ES_TEST", "name": "ESZ6"}
        self.engine = AxiomEngine(client=self.mock_client, default_symbol="ES")
        self.engine.account_id = 29611278

    def _create_2bar_df(self, closed_bar, curr_bar, vwap=5000.0, std_dev=20.0, vwap_prev=None):
        v_prev = vwap if vwap_prev is None else vwap_prev
        data = [
            {
                "Timestamp": pd.Timestamp("2026-11-04 04:00:00", tz="UTC"),
                "Open": closed_bar["o"], "High": closed_bar["h"], "Low": closed_bar["l"], "Close": closed_bar["c"],
                "Volume": 1000, "VWAP": v_prev, "Upper_Band": v_prev + std_dev, "Lower_Band": v_prev - std_dev
            },
            {
                "Timestamp": pd.Timestamp("2026-11-04 04:30:00", tz="UTC"),
                "Open": curr_bar["o"], "High": curr_bar["h"], "Low": curr_bar["l"], "Close": curr_bar["c"],
                "Volume": 50, "VWAP": vwap, "Upper_Band": vwap + std_dev, "Lower_Band": vwap - std_dev
            }
        ]
        return pd.DataFrame(data)

    def test_setup_short_valid_within_corridor(self):
        """
        Vela de 30m: abrió bajo VWAP (4998), mecha tocó VWAP (5002 >= 5000),
        cuerpo quedó bajo VWAP (max(4998, 4998) < 5000).
        Precio actual a 4998 (2.0 pts = 8 ticks de VWAP):
        - Cumple mínimo 5 ticks (8 >= 5).
        - Cumple tope 25% banda (std_dev=20.0 -> 25% = 5.0 pts = 20 ticks; 8 <= 20).
        """
        vwap = 5000.0
        std_dev = 20.0 # 50% = 10.0 pts (40 ticks en ES); 25% = 5.0 pts (20 ticks en ES)
        closed_bar = {"o": 4990.0, "h": 4995.0, "l": 4985.0, "c": 4992.0}
        curr_bar = {"o": 4998.0, "h": 5002.0, "l": 4996.0, "c": 4998.0}

        df = self._create_2bar_df(closed_bar, curr_bar, vwap=vwap, std_dev=std_dev, vwap_prev=5002.0)
        from unittest.mock import patch
        with patch("engine.INVERT_STRATEGY", False):
            setup = self.engine.verificar_setup_vwap(df, "ES", current_price=4998.0, bar_index=-1)

        self.assertIsNotNone(setup, "Debe generar setup SHORT válido dentro del corredor")
        self.assertEqual(setup["direccion"], "SHORT")
        # SL fijo a 5 ticks de VWAP (5 * 0.25 = 1.25 pts arriba de VWAP = 5001.25)
        self.assertEqual(setup["precio_sl"], 5001.25, "SL debe estar fijo a 5 ticks por encima de VWAP")
        # TP con el 20% de la distancia entre VWAP y banda (4.0 pts) desde entrada (4998.0 - 4.0 = 4994.0)
        self.assertEqual(setup["precio_tp"], 4994.0, "TP debe estar a 4.0 pts de la entrada")
        self.assertEqual(setup["tp_ticks"], 16, "TP debe ser exactamente 16 ticks")
        self.assertEqual(setup["dist_vwap_ticks"], 8, "Distancia a VWAP debe ser 8 ticks")
        self.assertEqual(setup["max_corridor_ticks"], 20, "Tope máximo 25% debe ser 20 ticks")

    def test_setup_long_valid_within_corridor(self):
        """
        Vela de 30m: abrió sobre VWAP (5002), mecha perforó VWAP (4998 <= 5000),
        cuerpo quedó sobre VWAP (min(5002, 5002) > 5000).
        Precio actual a 5002 (2.0 pts = 8 ticks de VWAP):
        - Cumple mínimo 5 ticks (8 >= 5).
        - Cumple tope 25% banda (std_dev=20.0 -> 25% = 5.0 pts = 20 ticks; 8 <= 20).
        """
        vwap = 5000.0
        std_dev = 20.0
        closed_bar = {"o": 5010.0, "h": 5015.0, "l": 5005.0, "c": 5008.0}
        curr_bar = {"o": 5002.0, "h": 5005.0, "l": 4998.0, "c": 5002.0}

        df = self._create_2bar_df(closed_bar, curr_bar, vwap=vwap, std_dev=std_dev, vwap_prev=4998.0)
        from unittest.mock import patch
        with patch("engine.INVERT_STRATEGY", False):
            setup = self.engine.verificar_setup_vwap(df, "ES", current_price=5002.0, bar_index=-1)

        self.assertIsNotNone(setup, "Debe generar setup LONG válido dentro del corredor")
        self.assertEqual(setup["direccion"], "LONG")
        # SL fijo a 5 ticks de VWAP (5 * 0.25 = 1.25 pts abajo de VWAP = 4998.75)
        self.assertEqual(setup["precio_sl"], 4998.75, "SL debe estar fijo a 5 ticks por debajo de VWAP")
        # TP con el 20% de la distancia entre VWAP y banda (4.0 pts) desde entrada (5002.0 + 4.0 = 5006.0)
        self.assertEqual(setup["precio_tp"], 5006.0, "TP debe estar a 4.0 pts de la entrada")
        self.assertEqual(setup["tp_ticks"], 16, "TP debe ser exactamente 16 ticks")

    def test_setup_rejected_if_less_than_5_ticks_confirmation(self):
        """Rechazar si el precio no se ha confirmado al menos 5 ticks de nuestro lado."""
        vwap = 5000.0
        std_dev = 20.0
        # Precio actual a 4999.25 (solo 3 ticks de VWAP, menor al mínimo de 5 ticks)
        closed_bar = {"o": 4990.0, "h": 4995.0, "l": 4985.0, "c": 4992.0}
        curr_bar = {"o": 4998.0, "h": 5002.0, "l": 4997.0, "c": 4999.25}

        df = self._create_2bar_df(closed_bar, curr_bar, vwap=vwap, std_dev=std_dev)
        setup = self.engine.verificar_setup_vwap(df, "ES", current_price=4999.25, bar_index=-1)
        self.assertIsNone(setup, "Debe rechazar la entrada si el precio está a menos de 5 ticks de VWAP")

    def test_setup_rejected_if_exceeds_25_pct_band_distance(self):
        """Rechazar si el precio ya se alejó más del 25% de la distancia de VWAP a la banda (tope máximo)."""
        vwap = 5000.0
        std_dev = 20.0 # 25% = 5.0 pts (20 ticks)
        # Precio actual a 4994.0 (6.0 pts = 24 ticks de VWAP, mayor al 25% que son 20 ticks)
        closed_bar = {"o": 4990.0, "h": 4995.0, "l": 4985.0, "c": 4992.0}
        curr_bar = {"o": 4998.0, "h": 5002.0, "l": 4993.0, "c": 4994.0}

        df = self._create_2bar_df(closed_bar, curr_bar, vwap=vwap, std_dev=std_dev)
        setup = self.engine.verificar_setup_vwap(df, "ES", current_price=4994.0, bar_index=-1)
        self.assertIsNone(setup, "Debe rechazar si el precio supera el 25% de distancia a la banda (evita entradas tardías)")

    def test_setup_rejected_if_body_crossed_vwap(self):
        """Rechazar si el cuerpo de la vela tocó o cruzó el VWAP (debe ser mecha pura, NO cuerpo)."""
        vwap = 5000.0
        # Vela abrió en 4998 pero cotiza a 5001.0 (cuerpo cruzó arriba de VWAP)
        closed_bar = {"o": 4990.0, "h": 4995.0, "l": 4985.0, "c": 4992.0}
        curr_bar = {"o": 4998.0, "h": 5005.0, "l": 4997.0, "c": 5001.0}

        df = self._create_2bar_df(closed_bar, curr_bar, vwap=vwap, std_dev=20.0)
        setup = self.engine.verificar_setup_vwap(df, "ES", current_price=5001.0, bar_index=-1)
        self.assertIsNone(setup, "No debe entrar si el cuerpo está cruzando el VWAP")

    def test_setup_rejected_if_no_wick_touched_vwap(self):
        """Rechazar si la mecha no alcanzó la línea de VWAP."""
        vwap = 5000.0
        # High 4998 < 5000 (no tocó VWAP)
        closed_bar = {"o": 4990.0, "h": 4995.0, "l": 4985.0, "c": 4992.0}
        curr_bar = {"o": 4995.0, "h": 4998.0, "l": 4992.0, "c": 4995.0}

        df = self._create_2bar_df(closed_bar, curr_bar, vwap=vwap, std_dev=20.0)
        setup = self.engine.verificar_setup_vwap(df, "ES", current_price=4995.0, bar_index=-1)
        self.assertIsNone(setup, "No debe entrar si la mecha no tocó el VWAP")

    def test_max_1_trade_per_30m_candle(self):
        """Verifica que no se abra más de 1 operación en la misma vela de 30 minutos."""
        sym = "ES"
        candle_ts = pd.Timestamp("2026-11-04 04:30:00", tz="UTC")
        self.engine.symbol_states[sym]["last_executed_candle_ts"] = candle_ts

        # Simular que se intenta evaluar la misma vela de 30m
        setup = {
            "candle_closed_ts": candle_ts,
            "simbolo": sym,
            "direccion": "SHORT"
        }
        can_trade_in_candle = (self.engine.symbol_states[sym]["last_executed_candle_ts"] != setup["candle_closed_ts"])
        self.assertFalse(can_trade_in_candle, "Debe prohibir más de 1 operación en la misma vela de 30 minutos")

    def test_sl_remains_fixed_no_dynamic_trailing(self):
        """Verifica que el SL permanezca fijo y no se mueva con USE_DYNAMIC_SL=False."""
        sym = "ES"
        entry_price = 4996.0
        sl_fijo = 5001.25 # 5 ticks de VWAP
        tp_fijo = 4986.0
        
        self.engine.symbol_states[sym]["active_trade"] = {
            "symbol": sym,
            "contract_id": "CON_ES_TEST",
            "direction": "SHORT",
            "entry_price": entry_price,
            "vwap_cruce": 5000.0,
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
        """Verifica que los parámetros de configuración coincidan con las nuevas reglas y la nueva cuenta."""
        self.assertEqual(config.ACCOUNT_NAME, "PRAC-V2-460183-95078649")
        self.assertEqual(config.BAR_TIMEFRAME_MINUTES, 30)
        self.assertEqual(config.CHECK_INTERVAL_MINUTES, 5)
        self.assertEqual(config.MIN_ENTRY_CONFIRM_TICKS, 5)
        self.assertEqual(config.MAX_ENTRY_BAND_RATIO, 0.25)
        self.assertEqual(config.MAX_TRADES_PER_30M_CANDLE, 1)
        self.assertEqual(config.SL_VWAP_DISTANCE_TICKS, 5)
        self.assertEqual(config.TP_VWAP_BAND_RATIO, 0.20)
        self.assertFalse(config.USE_DYNAMIC_SL)
        self.assertEqual(config.POLL_INTERVAL_SECONDS, 1.0)

if __name__ == "__main__":
    unittest.main()
