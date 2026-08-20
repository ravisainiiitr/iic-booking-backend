# Channel-I Department Mapping

```
Channel-I department string
        →  ChannelIDepartmentMapping
        →  Internal Department (FK)
```

Normalization: trim, collapse whitespace, casefold. No fuzzy matching.
Unmapped: students cannot choose an arbitrary HoD; Main Admin sees unmapped list.
Do not auto-create internal departments from Channel-I names when mapping is enabled.
Historical affiliations are not rewritten when Channel-I department text changes.
