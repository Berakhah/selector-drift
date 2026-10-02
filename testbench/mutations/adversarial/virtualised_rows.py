"""VirtualisedRows: the grid becomes virtualised -- the browser keeps only the first ten rows.

The one mutation that injects a script, and the only script any bench page ever carries (bench
spec §5). The served HTML keeps every row; after DOMContentLoaded the browser's DOM holds ten.
Every row the resolver sees is real and correctly placed, so no rung and no value check can
object -- only the row-count contract (Task 29) notices that 50 rows became 10. It is also why
the served-equals-tested test (Task 8) compares browser DOMs, not HTTP bodies.
"""

from __future__ import annotations

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.grid import data_rows, elements, grids

KEEP = 10
SCRIPT = (
    'document.addEventListener("DOMContentLoaded",function(){'
    'var r=Array.prototype.filter.call(document.querySelectorAll("tr,[role=row]"),'
    'function(x){return x.querySelector("td,[role=cell],[role=gridcell]")!==null});'
    f"r.slice({KEEP}).forEach(function(x){{x.remove()}})}});"
)


class VirtualisedRows(MutationBase):
    def apply(self, html: str) -> str:
        doc = parse(html)
        body = doc.find("body")
        has_rows = any(data_rows(grid) for grid in grids(doc))
        already = any(s.text == SCRIPT for s in elements(doc.xpath("//script")))
        if body is None or not has_rows or already:
            return serialise(doc)
        script = body.makeelement("script", {})
        script.text = SCRIPT
        body.append(script)
        return serialise(doc)
