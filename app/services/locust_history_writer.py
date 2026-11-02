"""Installed in the Locust child only, before the CLI constructs its CSV writer."""

import csv
import time


def configure_csv_history():
    from locust import stats

    stats.CSV_STATS_INTERVAL_SEC = 1
    stats.CSV_STATS_FLUSH_INTERVAL_SEC = 1
    if getattr(stats.StatsCSVFileWriter, "_sqlpulse_final_history", False):
        return

    class FinalHistoryWriter(stats.StatsCSVFileWriter):
        _sqlpulse_final_history = True

        def close_files(self):
            if self.stats_history_csv_filehandle.closed:
                return
            try:
                # Locust's normal close only flushes buffered rows. Capture the actual
                # terminal stats, after users stop, instead of losing the last interval.
                self._stats_history_data_rows(self.stats_history_csv_writer, time.time())
                for handle, write in (
                    (self.requests_csv_filehandle, self.requests_csv),
                    (self.failures_csv_filehandle, self.failures_csv),
                    (self.exceptions_csv_filehandle, self.exceptions_csv),
                ):
                    handle.seek(0)
                    write(csv.writer(handle))
                    handle.truncate()
            finally:
                super().close_files()

    stats.StatsCSVFileWriter = FinalHistoryWriter
