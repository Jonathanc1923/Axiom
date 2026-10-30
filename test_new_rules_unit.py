import unittest
import pandas as pd
import numpy as np
import time
from unittest.mock import MagicMock
from engine import AxiomEngine
import config

class TestNewVwapIdea(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.account_id = 12345
        self.mock_client.get_open_positions.return_value = []
        self.mock_client.search_open_orders.return_value = []
        self.mock_client.resolve_contract.return_value = {"id": "CON_ES_TEST", "name": "ESZ6"}
        self.engine = AxiomEngine(client=self.mock_client, default_symbol="ES")
        self.engine.account_id = 12345

    def _create_3bar_df(self, t2, t1, t0, vwap=5000.0, std_dev=20.0):
        # t2: t-2, t1: t-1, t0: t (current)
        data = [
            {
                "Timestamp": pd.Timestamp("2026-10-29 10:00:00", tz="UTC"),
                "Open": t2["o"], "High": t2["h"], "Low": t2["l"], "Close": t2["c"],
                "Volume": 1000, "VWAP": vwap, "Upper_Band": vwap + std_dev, "Lower_Band": vwap - std_dev
            },
            {
                "Timestamp": pd.Timestamp("2026-10-29 10:05:00", tz="UTC"),
                "Open": t1["o"], "High": t1["h"], "Low": t1["l"], "Close": t1["c"],
                "Volume": 1000, "VWAP": vwap, "Upper_Band": vwap + std_dev, "Lower_Band": vwap - std_dev
            },
            {
                "Timestamp": pd.Timestamp("2026-10-29 10:10:00", tz="UTC"),
                "Open": t0["o"], "High": t0["h"], "Low": t0["l"], "Close": t0["c"],
                "Volume": 100, "VWAP": vwap, "Upper_Band": vwap + std_dev, "Lower_Band": vwap - std_dev
            }
        ]
        return pd.DataFrame(data)

    def test_setup_short_valid(self):
        """Vela t-2 completa bajo VWAP, Vela t-1 mecha toca VWAP y cuerpo bajo VWAP, Vela t abre bajo VWAP."""
        vwap = 5000.0
        # t-2: completely below VWAP (High 4995 < 5000)
        t2 = {"o": 4990.0, "h": 4995.0, "l": 4985.0, "c": 4992.0}
        # t-1: wick touches VWAP (High 5002 >= 5000), body below (Open 4992, Close 4996 < 5000)
        t1 = {"o": 4992.0, "h": 5002.0, "l": 4990.0, "c": 4996.0}
        # t: opens below VWAP (Open 4996 < 5000)
        t0 = {"o": 4996.0, "h": 4997.0, "l": 4994.0, "c": 4995.0}

        df = self._create_3bar_df(t2, t1, t0, vwap=vwap, std_dev=20.0)
        setup = self.engine.verificar_setup_vwap(df, "ES", current_price=4995.0)

        self.assertIsNotNone(setup, "Debe generar setup SHORT")
        self.assertEqual(setup["direccion"], "SHORT")
        # SL: 25% de 20.0 std_dev = 5.0 pts sobre VWAP = 5005.0 (mayor musculo de aguante)
        self.assertEqual(setup["precio_sl"], 5005.0, "SL debe estar al 25% de distancia del VWAP hacia la banda superior")
        # TP: 60% de 20.0 std_dev = 12.0 pts bajo VWAP = 4988.0
        self.assertEqual(setup["precio_tp"], 4988.0, "TP debe estar al 60% de distancia hacia banda inferior")

    def test_setup_long_valid(self):
        """Vela t-2 completa sobre VWAP, Vela t-1 mecha toca VWAP y cuerpo sobre VWAP, Vela t abre sobre VWAP."""
        vwap = 5000.0
        # t-2: completely above VWAP (Low 5005 > 5000)
        t2 = {"o": 5010.0, "h": 5015.0, "l": 5005.0, "c": 5008.0}
        # t-1: wick touches VWAP (Low 4998 <= 5000), body above (Open 5008, Close 5004 > 5000)
        t1 = {"o": 5008.0, "h": 5012.0, "l": 4998.0, "c": 5004.0}
        # t: opens above VWAP (Open 5004 > 5000)
        t0 = {"o": 5004.0, "h": 5006.0, "l": 5003.0, "c": 5005.0}

        df = self._create_3bar_df(t2, t1, t0, vwap=vwap, std_dev=20.0)
        setup = self.engine.verificar_setup_vwap(df, "ES", current_price=5005.0)

        self.assertIsNotNone(setup, "Debe generar setup LONG")
        self.assertEqual(setup["direccion"], "LONG")
        # SL: 25% de 20.0 std_dev = 5.0 pts bajo VWAP = 4995.0 (mayor musculo de aguante)
        self.assertEqual(setup["precio_sl"], 4995.0, "SL debe estar al 25% de distancia del VWAP hacia la banda inferior")
        # TP: 60% de 20.0 std_dev = 12.0 pts sobre VWAP = 5012.0
        self.assertEqual(setup["precio_tp"], 5012.0, "TP debe estar al 60% de distancia hacia banda superior")

    def test_setup_rejected_if_body_crossed_vwap(self):
        """Rechazar si el cuerpo de la vela t-1 cerró al otro lado de VWAP."""
        vwap = 5000.0
        t2 = {"o": 4990.0, "h": 4995.0, "l": 4985.0, "c": 4992.0}
        # t-1: cuerpo cruzó VWAP (Close 5003 > 5000)
        t1 = {"o": 4992.0, "h": 5005.0, "l": 4990.0, "c": 5003.0}
        t0 = {"o": 5003.0, "h": 5004.0, "l": 5001.0, "c": 5002.0}

        df = self._create_3bar_df(t2, t1, t0, vwap=vwap, std_dev=20.0)
        setup = self.engine.verificar_setup_vwap(df, "ES", current_price=5002.0)
        self.assertIsNone(setup, "No debe entrar si el cuerpo de la vela cruzó el VWAP")

    def test_setup_rejected_if_candle_t2_not_clean(self):
        """Rechazar si la vela t-2 ya estaba tocando o cruzando el VWAP."""
        vwap = 5000.0
        # t-2: tocó VWAP (High 5001 >= 5000)
        t2 = {"o": 4990.0, "h": 5001.0, "l": 4985.0, "c": 4992.0}
        t1 = {"o": 4992.0, "h": 5002.0, "l": 4990.0, "c": 4996.0}
        t0 = {"o": 4996.0, "h": 4997.0, "l": 4994.0, "c": 4995.0}

        df = self._create_3bar_df(t2, t1, t0, vwap=vwap, std_dev=20.0)
        setup = self.engine.verificar_setup_vwap(df, "ES", current_price=4995.0)
        self.assertIsNone(setup, "No debe entrar si la vela t-2 no estuvo 100% limpia de su lado")

    def test_setup_rejected_if_curr_candle_opened_past_vwap(self):
        """Rechazar si la vela actual t abrió pasando el VWAP."""
        vwap = 5000.0
        t2 = {"o": 4990.0, "h": 4995.0, "l": 4985.0, "c": 4992.0}
        t1 = {"o": 4992.0, "h": 5002.0, "l": 4990.0, "c": 4996.0}
        # t0: abrió sobre VWAP (Open 5001 > 5000)
        t0 = {"o": 5001.0, "h": 5003.0, "l": 4999.0, "c": 5000.0}

        df = self._create_3bar_df(t2, t1, t0, vwap=vwap, std_dev=20.0)
        setup = self.engine.verificar_setup_vwap(df, "ES", current_price=5000.0)
        self.assertIsNone(setup, "No debe entrar si la vela actual abrió pasando el VWAP")

    def test_setup_rejected_if_price_already_reached_tp(self):
        """Caso exacto de YM: Rechazar si el rebote fue tan fuerte que el precio ya alcanzó o superó el TP."""
        vwap = 52502.0
        std_dev = 66.47
        # TP objetivo = 52502 + (0.60 * 66.47) = 52541.88
        # Vela t-2: limpia arriba
        t2 = {"o": 52515.0, "h": 52520.0, "l": 52510.0, "c": 52518.0}
        # Vela t-1: mecha tocó VWAP (Low 52501), pero cuerpo rebotó salvajemente hasta 52543 (ya superando el TP)
        t1 = {"o": 52518.0, "h": 52544.0, "l": 52501.0, "c": 52543.0}
        # Vela t: abre en 52543 (por encima del TP 52542)
        t0 = {"o": 52543.0, "h": 52546.0, "l": 52542.0, "c": 52543.0}

        df = self._create_3bar_df(t2, t1, t0, vwap=vwap, std_dev=std_dev)
        setup = self.engine.verificar_setup_vwap(df, "YM", current_price=52543.0)
        self.assertIsNone(setup, "Debe rechazar la entrada porque el precio ya rebasó el TP teórico antes de entrar")

    def test_setup_rejected_if_late_entry_consumes_over_50pct_tp(self):
        """Rechazar si el precio de entrada ya consumió más del 50% de la distancia al TP dejando R:R pobre."""
        vwap = 5000.0
        std_dev = 20.0
        # TP = 5012.0 (12 pts desde VWAP). 50% de 12 pts = 6.0 pts.
        # Si la vela abre a 5008.0 (8.0 pts consumidos = 66.7% > 50%), debe ser descartada.
        t2 = {"o": 5010.0, "h": 5015.0, "l": 5005.0, "c": 5008.0}
        t1 = {"o": 5008.0, "h": 5010.0, "l": 4998.0, "c": 5008.0}
        t0 = {"o": 5008.0, "h": 5009.0, "l": 5007.0, "c": 5008.0}

        df = self._create_3bar_df(t2, t1, t0, vwap=vwap, std_dev=std_dev)
        setup = self.engine.verificar_setup_vwap(df, "ES", current_price=5008.0)
        self.assertIsNone(setup, "Debe descartar si el precio ya consumió más del 50% de la ganancia al TP")

    def test_dinamismo_be_plus_2_ticks_long(self):
        """Para LONG: Al superar 50% de ganancia hacia el TP, el SL debe moverse a VWAP + 2 ticks a favor."""
        sym = "ES"
        entry_price = 5004.0
        vwap = 5000.0
        initial_sl = 4995.0 # 25% banda
        initial_tp = 5016.0 # 12 pts TP
        
        self.engine.symbol_states[sym]["active_trade"] = {
            "symbol": sym,
            "contract_id": "CON_ES_TEST",
            "direction": "LONG",
            "entry_price": entry_price,
            "vwap_cruce": vwap,
            "entry_timestamp": time.time(),
            "initial_sl_price": initial_sl,
            "current_sl_price": initial_sl,
            "initial_tp_price": initial_tp,
            "current_tp_price": initial_tp,
            "base_tp_pts": 12.0,
            "d_tp_ini": 12.0,
            "be_plus_2_active": False,
            "sl_stage": 0.0,
            "highest_price": entry_price,
            "lowest_price": entry_price
        }
        self.engine.symbol_states[sym]["has_open_position"] = True

        self.mock_client.get_open_positions.return_value = [{"contractId": "CON_ES_TEST", "size": 1}]
        self.mock_client.search_open_orders.return_value = [
            {"id": 101, "contractId": "CON_ES_TEST", "type": 4, "stopPrice": initial_sl, "side": 1}
        ]
        self.mock_client.modify_order.return_value = {"success": True}

        # 1. Precio avanza a 5008.0 (4 pts = 33% de TP): No debe mover aún
        self.engine.monitorear_posicion_abierta(sym, current_price=5008.0, cached_positions=[{"contractId": "CON_ES_TEST", "size": 1}])
        trade = self.engine.symbol_states[sym]["active_trade"]
        self.assertFalse(trade.get("be_plus_2_active", False))
        self.assertEqual(trade["current_sl_price"], initial_sl)

        # 2. Precio avanza a 5011.0 (7.0 pts = 58% de TP >= 50%): Debe activar BE + 2 ticks
        # VWAP 5000.0 + (2 * 0.25) = 5000.50
        self.engine.monitorear_posicion_abierta(sym, current_price=5011.0, cached_positions=[{"contractId": "CON_ES_TEST", "size": 1}])
        self.assertTrue(trade.get("be_plus_2_active", False), "Debe marcar BE+2t activo")
        self.assertEqual(trade["current_sl_price"], 5000.50, "SL debe estar en VWAP + 2 ticks (5000.50)")
        self.mock_client.modify_order.assert_called_with(
            account_id=12345,
            order_id=101,
            stop_price=5000.50,
            size=1
        )

    def test_dinamismo_be_plus_2_ticks_short(self):
        """Para SHORT: Al superar 50% de ganancia hacia el TP, el SL debe moverse a VWAP - 2 ticks a favor."""
        sym = "ES"
        entry_price = 4996.0
        vwap = 5000.0
        initial_sl = 5005.0 # 25% banda
        initial_tp = 4984.0 # 12 pts TP
        
        self.engine.symbol_states[sym]["active_trade"] = {
            "symbol": sym,
            "contract_id": "CON_ES_TEST",
            "direction": "SHORT",
            "entry_price": entry_price,
            "vwap_cruce": vwap,
            "entry_timestamp": time.time(),
            "initial_sl_price": initial_sl,
            "current_sl_price": initial_sl,
            "initial_tp_price": initial_tp,
            "current_tp_price": initial_tp,
            "base_tp_pts": 12.0,
            "d_tp_ini": 12.0,
            "be_plus_2_active": False,
            "sl_stage": 0.0,
            "highest_price": entry_price,
            "lowest_price": entry_price
        }
        self.engine.symbol_states[sym]["has_open_position"] = True

        self.mock_client.get_open_positions.return_value = [{"contractId": "CON_ES_TEST", "size": -1}]
        self.mock_client.search_open_orders.return_value = [
            {"id": 102, "contractId": "CON_ES_TEST", "type": 4, "stopPrice": initial_sl, "side": 0}
        ]
        self.mock_client.modify_order.return_value = {"success": True}

        # Precio cae a 4989.0 (7.0 pts a favor = 58% de TP >= 50%): Debe activar BE + 2 ticks a favor
        # VWAP 5000.0 - (2 * 0.25) = 4999.50
        self.engine.monitorear_posicion_abierta(sym, current_price=4989.0, cached_positions=[{"contractId": "CON_ES_TEST", "size": -1}])
        trade = self.engine.symbol_states[sym]["active_trade"]
        self.assertTrue(trade.get("be_plus_2_active", False), "Debe marcar BE+2t activo")
        self.assertEqual(trade["current_sl_price"], 4999.50, "SL debe estar en VWAP - 2 ticks (4999.50 a favor de SHORT)")
        self.mock_client.modify_order.assert_called_with(
            account_id=12345,
            order_id=102,
            stop_price=4999.50,
            size=1
        )

    def test_intervalo_de_monitoreo_es_un_segundo(self):
        """Verifica que el intervalo de latido del motor sea estrictamente de 1.0 segundo."""
        self.assertEqual(config.POLL_INTERVAL_SECONDS, 1.0, "El intervalo central debe ser 1.0 segundo")

if __name__ == "__main__":
    unittest.main()
