# Safe test containers

How to stand up a Listenarr instance to measure something, on a machine that is also running one
for real.

This is written down because the alternative is that each person, or each session, rediscovers the
same half dozen traps, and because one of them has already caused an incident here. Follow it even
when the test looks trivial. Most of the rules below exist because something that looked trivial
was not.

`tools/probe.sh` implements all of this. Prefer it to hand-rolled commands; the point of the script
is that doing it safely costs one line.

    tools/probe.sh up
    tools/probe.sh api <name> GET /configuration/startupconfig
    tools/probe.sh down <name>

## The hard rules

**Never `--network=host`.** It contends for the real instance's port, and the live instance will
answer as though it were the one you started. Every result you then collect is about production.

**Never bind the default port.** That belongs to the running install. Pick something well away
from it.

**Prove your port is free before you bind it.** `ss -ltn | grep ":<port> "` must return nothing. Do
not assume a port is free because you have not used it.

**A port answering is evidence of nothing** until you have matched it to the container you started:

    podman port <name>

This matters more than it sounds. A production instance may not appear in `podman ps` at all, so
podman cannot tell you who owns a port. Something answering on the port you expected is not proof
that it is yours.

**Never issue a write until the instance is confirmed yours.** Reads against the wrong instance
waste your time. Writes against the wrong instance destroy someone's data.

**Own network, own config directory, own name.** No sharing a volume, a database or a port with
anything else, including another test.

**Tear down when you are done,** and confirm as you go: the real instance still has its listeners,
your port has none.

## Use the stock image unless you mean not to

Anything you intend to report upstream has to reproduce on a stock build. A patched build is not a
control for a claim about the released software, and a report filed from one hands the maintainer a
defect he cannot reproduce.

Confirm rather than assume:

    podman exec <name> grep -o 'Listenarr\.Api/[0-9][^"]*' /app/Listenarr.Api.deps.json

A bare version is stock. A build-metadata suffix means it is a patched build and is not a control.
Note the running application truncates that suffix in its own API responses, so asking the API what
version it is will tell you it is stock when it is not. Read the dependency manifest, not the API.

## Traps that have already cost time

**A request through a mapped port is not loopback.** The application sees the connection coming
from the container network, not from itself. Code that treats loopback differently from other
private addresses will take the other branch. To get a genuinely loopback caller, run the request
from inside the container. `tools/probe.sh local` does this.

This distinction decided a finding: a secret came back redacted from the host through a mapped port
and in full from inside the container, and reading only the first result would have produced the
wrong conclusion, twice over.

**Writes need an antiforgery token.** Without one you get HTTP 400 and
`{"message":"Invalid or missing CSRF token"}`, which looks like the endpoint refusing you rather
than a missing header. Fetch `/api/v1/antiforgery/token` with a cookie jar and send the token back
in `X-XSRF-TOKEN` with the same jar. `tools/probe.sh api` handles it.

**config.json is not where the volume is mounted.** Inside the container it lives at
`/app/config/config.json`. Looking in the mount point and finding nothing is not evidence that
nothing was written.

**The image is minimal.** There is no `curl` and no `wget` inside it. There is `python3`.

## Controls, which are the whole point

A test whose apparatus failing looks the same as a pass proves nothing.

Before trusting any result, ask: if this were broken in the way I am claiming, and also if it were
not, would I see a different thing? If the answer is no, the test needs a control that must come
out differently.

A worked example from this repository. Checking whether a corrupted API key still authenticated,
the first run returned HTTP 200 for the corrupted key. It also returned HTTP 200 for a deliberately
wrong key, and for no key at all, because authentication was switched off on that instance. The
200 meant nothing. With authentication enabled the same three requests returned 401, 401 and 200,
and only then did the result say anything.

State the control alongside the result whenever you report one. "The corrupted key returned 200"
is an anecdote. "No key gave 401, a wrong key gave 401, the corrupted key gave 200" is a
measurement.

## Say which grade of evidence you have

When reporting, mark each claim:

- **Measured.** Observed on a running instance, with the control stated.
- **Read.** Established from the source, with file and line, but never executed.
- **Inferred.** Neither. Reasoning from how the pieces fit.

All three are legitimate and inferred claims are often right. The failure is not labelling them,
because a claim stated flatly has to be retracted when it turns out wrong, while a claim labelled
as inference is corrected in a sentence. Both have happened here within a single day, and the
difference in cost was large.
