import json
import unittest
from pathlib import Path


class AnalysisInputsTests(unittest.TestCase):
    def load_data(self):
        return json.loads((Path(__file__).resolve().parents[1] / "fixtures/analysis/input.json").read_text())

    def test_late_products(self):
        data = self.load_data()
        self.assertTrue(any(row['available_at'] > row['issued_at'] for row in data['products']))

    def test_truth_revision(self):
        data = self.load_data()
        self.assertTrue(any(row['revision'] > 1 for row in data['truth']))

    def test_partial_target(self):
        data = self.load_data()
        self.assertTrue(any(row['model'] == 'partial' for row in data['products']))

    def test_timely_baseline(self):
        data = self.load_data()
        self.assertTrue(any(row['model'] == 'fast' and row['available_at'] == row['issued_at'] for row in data['products']))

    def test_source_versions_and_cutoff(self):
        data = self.load_data()
        self.assertEqual(data["schema_version"], 1)
        self.assertTrue(data["source_versions"])
        self.assertIn("+00:00", data["as_of"])
