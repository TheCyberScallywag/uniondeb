# Incidents

This system holds allegations about named people. A leak, a wrong disclosure, or access by someone
who should not have had it harms them first. Speed and a clear record matter more than a tidy fix.

**First hour**

1. **Contain.** Revoke the session or account involved at the identity provider. If the route is
   unknown, stop the `api` service; ingest and scans can continue.
2. **Preserve.** Do not delete anything. Take a backup, and export the audit events for the period
   (`GET /audit/events` as an auditor).
3. **Tell** the governance owner and the data-protection lead. If a case, a flag or a SAR is
   involved, tell the nominated officer: confidentiality of an investigation is a legal matter
   (plan G12), and whether anyone outside is told is their decision with legal advice, never an
   engineer's.
4. **Start the clock.** A personal-data breach may have to be reported to the ICO within 72 hours of
   becoming aware of it. The data-protection lead decides; give them the time you became aware.

**What the system can tell you**

- Who looked at what: every case view, identity reveal, review item opened, Holwr question and
  export is an audit event with the actor and time. Filter by `actor` or `case_id`.
- Whether the record itself can be trusted: `uniondeb audit verify`.
- What an agent was shown: each tag and alert traces through provenance to the extract it was made
  from and that extract's contract version.

**Never**

- Tell the subject of a case, or anyone not assigned to it, that the case exists.
- Answer a subject access request for someone with an open case without the data-protection lead.
- Edit audit events, tags or provenance to correct a mistake. Corrections are new records.
