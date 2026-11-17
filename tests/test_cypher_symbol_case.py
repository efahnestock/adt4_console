"""Node symbols are stored upper case ("T3349", "O710", "R10"), but operators type
them the way the planner prints them ("t3349"). On the 2026-11-17 bench test,
"SGET: add a box at place t3349" ran MATCH (p:MeshPlace {nodeSymbol: 't3349'}),
found nothing, and the chat told the operator the place does not exist."""

from types import SimpleNamespace as NS

from adt4_console import cypher_tools
from adt4_console.cypher_tools import upper_symbol_literals


def test_a_lowercase_symbol_in_a_property_map_is_upper_cased():
    q = "MATCH (p:MeshPlace {nodeSymbol: 't3349'}) RETURN p.center AS center"
    assert upper_symbol_literals(q) == (
        "MATCH (p:MeshPlace {nodeSymbol: 'T3349'}) RETURN p.center AS center"
    )


def test_comparisons_and_lists_are_upper_cased():
    q = (
        'MATCH (o:Object) WHERE o.nodeSymbol = "o710" OR o.nodeSymbol IN '
        "['o536', \"r10\", 'T7375'] RETURN o"
    )
    assert upper_symbol_literals(q) == (
        'MATCH (o:Object) WHERE o.nodeSymbol = "O710" OR o.nodeSymbol IN '
        "['O536', \"R10\", 'T7375'] RETURN o"
    )


def test_other_strings_are_left_alone():
    q = (
        "MATCH (o:Object {class: 'box', name: 'b2'}) "
        "WHERE o.nodeSymbol STARTS WITH 'o' RETURN o"
    )
    assert upper_symbol_literals(q) == q


def test_run_read_runs_the_upper_cased_query():
    ran = []

    class Tx:
        def run(self, query):
            ran.append(query)
            return NS(__iter__=lambda self: iter(()), keys=lambda: [])

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute_read(self, work):
            return work.__wrapped__(Tx()) if hasattr(work, "__wrapped__") else work(Tx())

    src = NS(db=lambda: NS(driver=NS(session=lambda **kw: Session())))
    cypher_tools.run_read(src, "MATCH (p {nodeSymbol: 't3349'}) RETURN p")
    assert ran == ["MATCH (p {nodeSymbol: 'T3349'}) RETURN p"]
