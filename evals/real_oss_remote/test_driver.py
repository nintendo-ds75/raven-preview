"""Guard measurements against the timing bugs encountered in the first run."""
import json
import unittest

from .run_host import trace_state


def request(ident, name, when=0):
    return {"time": when, "direction": "request", "message": {
        "id": ident, "method": "tools/call", "params": {"name": name, "arguments": {}}}}


def response(ident, value, when=1):
    return {"time": when, "direction": "response", "message": {"id": ident, "result": {
        "content": [{"type": "text", "text": json.dumps(value)}], "isError": False}}}


class MeasurementTests(unittest.TestCase):
    def test_overlapping_sessions_do_not_pair_identical_rpc_ids(self):
        a={**request(1,'bridge_wait'),'session':'a'}
        b={**request(1,'bridge_get_tree'),'session':'b'}
        reply={**response(1,{'nodes':[]}), 'session':'b'}
        complete, waits=trace_state([a,b,reply])
        self.assertEqual(complete[0][0]['message']['params']['name'],'bridge_get_tree')
        self.assertEqual(waits,[a])

    def test_inflight_node_is_not_counted_and_finished_wait_is_not_active(self):
        trace = [request(1, "bridge_add_node"), request(2, "bridge_wait"), response(2, {})]
        complete, waits = trace_state(trace)
        self.assertEqual([r[0]["message"]["params"]["name"] for r in complete], ["bridge_wait"])
        self.assertEqual(waits, [])
        trace += [response(1, {"node_id": "ready", "owner": "final route"}), request(3, "bridge_wait")]
        complete, waits = trace_state(trace)
        self.assertEqual(complete[-1][2]["owner"], "final route")
        self.assertEqual([r["message"]["id"] for r in waits], [3])

    def test_restart_cannot_pair_a_response_with_an_old_request_id(self):
        trace = [request(1, "bridge_add_node"), {"direction": "request", "time": 1,
                 "message": {"method": "initialize", "id": 0}}, response(1, {"node_id": "wrong"}),
                 request(1, "bridge_get_tree"), response(1, {"nodes": []})]
        complete, _ = trace_state(trace)
        self.assertEqual(len(complete), 1)
        self.assertEqual(complete[0][0]["message"]["params"]["name"], "bridge_get_tree")

    def test_error_ends_wait_without_becoming_a_successful_measurement(self):
        trace = [request(9, "bridge_wait"), {"direction": "response", "time": 3,
                 "message": {"id": 9, "error": {"message": "connection lost"}}}]
        self.assertEqual(trace_state(trace), ([], []))


if __name__ == "__main__":
    unittest.main()
