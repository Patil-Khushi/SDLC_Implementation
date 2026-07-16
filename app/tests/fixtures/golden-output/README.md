# golden-output/ — expected generated files

Holds the expected source files a work item should produce, for byte/structure comparison in the
integration suite. Populated alongside the recorded LLM responses (`../llm-responses/`): once a
work item's generation is recorded and verified to compile in the sandbox, its output is captured
here as the golden reference for later runs.

Empty until the record pass has been run.
