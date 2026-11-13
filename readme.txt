测试：python3 -m unittest discover -s tests/forecast_vintages -p 'test_*.py'
构建：python3 -m compileall -q progressive_forecast_chain tests/forecast_vintages
验收：python3 -m progressive_forecast_chain.release.cli fixtures/demo.json --database /tmp/progressive_forecast_chain-acceptance.sqlite3
