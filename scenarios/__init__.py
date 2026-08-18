"""Three debugging scenarios — what you put on the screen.

Each one reproduces from scratch with one command and rests on defects planted
into the raw layer by the generator rather than staged for the demo. Nothing has
to be broken on purpose: the late return, the partition that never arrived and
the broken counter are already in the data, and the scenarios do not assign
their dates, they ask the defect map.

    make scenario-late-return        yesterday's number moved, and you can see why
    make scenario-missing-partition  the source did not arrive, the chain stopped
    make scenario-broken-counter     the device lies, the network still counts

The scenarios run the pipeline with the same commands a person would use —
`make run`, not a walk over the models of their own. Otherwise the demo would be
proving that the demo works.

Two operations do go straight to the database, and both are in the late-return
scenario. It winds time back, and the pipeline's own commands cannot do that:
the pipeline knows how to compute forward, not how to forget. So the scenario
deletes the returns that "have not arrived yet" from the raw layer and clears
the mart snapshot, leaving the revision log to fill from nothing before your
eyes. Both operations are named out loud in the scenario's output, and the
replay itself puts back what was deleted.

A scenario is able to fail: if a run did not end the way it promised, or the
mart changed where it should not have, the command exits non-zero.
"""
