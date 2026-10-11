import copy
from datetime import date, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import refresh_prices_manual as refresh
import risk_refresh as risk

class RecoveryTests(unittest.TestCase):
    def option(self):
        return {'underlying':'AAA','contracts':2,'multiplier':100,'position_side':'SHORT',
                'option_type':'CALL','expiration':'2027-01-15','strike':'10'}

    def test_exact_option_identity_and_signed_value(self):
        h={'supplemental':{'option_contracts':[self.option()]}}
        original=copy.deepcopy(h)
        out,n,_=refresh.revalue_options(h,lambda s,expected_type: {'price':'1.25','as_of':'2026-10-09T20:00:00+00:00'})
        self.assertEqual(n,1)
        self.assertEqual(out['supplemental']['option_contracts'][0]['market_value'],'-250.00')
        self.assertEqual(refresh.option_symbol(self.option()),'AAA270115C00010000')
        self.assertEqual(h,original)

    def test_adjusted_deliverable_and_conflicting_identity_rejected(self):
        for changes in ({'multiplier':50},{'broker_symbol':'BBB270115C00010000'},{'position_side':'UNKNOWN'}):
            o=self.option();o.update(changes)
            with self.assertRaises(ValueError): refresh.option_symbol(o)

    def test_older_option_trade_retains_saved_mark(self):
        o=self.option();o.update(quote_as_of='2026-10-09T20:00:00Z',market_value='-300')
        h={'supplemental':{'option_contracts':[o]}}
        out,n,_=refresh.revalue_options(h,lambda s,expected_type: {'price':'1','as_of':'2026-10-08T20:00:00+00:00'})
        self.assertEqual(n,0)
        self.assertEqual(out['supplemental']['option_contracts'][0]['market_value'],'-300')

    def holdings(self):
        return {'snapshot_id':'synthetic','as_of':'2026-10-09T20:00:00Z',
                'accounts':[{'account_id':'test','cash':'0','currency':'USD'}],
                'positions':[{'account_id':'test','instrument_id':'AAA','asset_type':'STOCK','currency':'USD','quantity':'2'}]}

    def test_missing_mark_withholds_value_and_quantity_stays_unchanged(self):
        h=self.holdings();old=copy.deepcopy(h)
        out,n=refresh.revalue(h,{}, {})
        self.assertEqual(n,0)
        self.assertIsNone(out['risk']['positions'][0]['market_value'])
        self.assertEqual(h,old)

    def test_no_implicit_currency_conversion(self):
        h=self.holdings();h['accounts'][0]['currency']='EUR';h['positions'][0]['currency']='EUR'
        i,_=refresh.revalue(h,{}, {'AAA':{'price':'10','as_of':'2026-10-09T20:00:00+00:00','source':'synthetic'}})
        self.assertIsNone(i['risk']['marked_market_value'])
        self.assertIsNone(refresh.history_sample(h,i,'2026-10-09T21:00:00Z',None))

    def test_current_weight_risk_reproduces_identical_benchmark(self):
        h=self.holdings();i,_=refresh.revalue(h,{}, {'AAA':{'price':'10','as_of':'2026-10-09T20:00:00+00:00','source':'synthetic'}})
        series={(date(2025,1,1)+timedelta(days=k)).isoformat():100*(1.01 if k%2 else 1)*(1.001**k) for k in range(80)}
        out=risk.calculate(h,i,{'AAA':series,'^GSPC':series})
        self.assertAlmostEqual(out['data']['books'][0]['beta'],1)
        self.assertAlmostEqual(out['data']['books'][0]['corr'],1)
        self.assertIn('Options and Principal excluded',out['data']['books'][0]['note'])

    def test_risk_rejects_snapshot_mismatch(self):
        with self.assertRaises(ValueError):risk.calculate(self.holdings(),{'holdings_snapshot_id':'different'}, {})

if __name__=='__main__':unittest.main()
