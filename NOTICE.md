# am-party

am-party (party jukebox). This file lists third-party code, models and notable
dependencies that am-party includes or ships in its container image. The full text of the
Apache License 2.0, under which the Music Assistant code below is used, is in
[LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt).

## Apple Music provider (backend/am_party/providers/apple_music/)

Portions of this module are adapted from Music Assistant
(https://github.com/music-assistant/server, release 2.10.8), licensed under the
Apache License, Version 2.0 (https://www.apache.org/licenses/LICENSE-2.0).
Copyright the Music Assistant contributors.

Adapted files: api_client.py, streaming.py, parsers.py, client.py (from provider.py /
media.py / library.py), auth.py (from setup_flow.py), errors.py, helpers/utils.py,
helpers/throttle_retry.py (from music_assistant/helpers/throttle_retry.py, which embeds
asyncio-throttle by hallazzang, MIT), musickit_auth/musickit_wrapper.html.
Modifications: all Music Assistant internals (models, cache, webserver, setup flow,
config) were removed and replaced by plain aiohttp/asyncio code returning am-party's own
types; see the header comment of each file for specifics. musickit_wrapper.css is a new
stylesheet (MA's copy of Apple's stylesheet was not vendored).

helpers/throttle_retry.py embeds asyncio-throttle (https://github.com/hallazzang/asyncio-throttle):

```
The MIT License (MIT)

Copyright (c) 2017-2019 Hanjun Kim

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Smart fades (backend/am_party/smartfades/)

Adapted from Music Assistant "smart fades" (https://github.com/music-assistant/server,
release 2.10.8), licensed under the Apache License, Version 2.0
(https://www.apache.org/licenses/LICENSE-2.0). Copyright the Music Assistant contributors.

Adapted files: mixing/** (from music_assistant/controllers/streams/smart_fades/),
analysis/** (from music_assistant/providers/smart_fades/, provider.py -> analyzer.py),
audio_analysis.py (from music_assistant/models/audio_analysis.py), pcm.py (pieces of
music_assistant/helpers/audio.py and helpers/ffmpeg.py). Tests under
backend/tests/smartfades/ are ported from tests/controllers/streams/smart_fades/.
Modifications: Music Assistant internals (mass, config entries, cache/DB, StreamDetails,
AsyncProcess, AudioFormat, analysis thread pool and playback pacing, mashumaro) were
removed or replaced by small local equivalents; the planning, rendering and analysis
algorithms are unchanged. worker.py and transition.py are new am-party glue code. Each
adapted file carries a header naming its origin.

The smart fades code redistributes the following third-party components (attributions
copied from Music Assistant's NOTICE):

-------------------------------------------------------------------------------

Beat This! - Beat Tracking Model
https://github.com/CPJKU/beat_this
(used as the `beat-this` Python package; the "small0" checkpoint is downloaded from the
authors' server at image build time)

MIT License

Copyright (c) 2024 Institute of Computational Perception, JKU Linz, Austria

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

-------------------------------------------------------------------------------

SKey - Musical Key Estimation Model
https://github.com/deezer/skey
(model code and weights vendored in backend/am_party/smartfades/analysis/resources/
skey_model.py and skey.pt)

MIT License

Copyright (c) 2019-present, Deezer SA.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

-------------------------------------------------------------------------------

FireRedVAD - Audio Event Detection Model
https://github.com/FireRedTeam/FireRedVAD
Model: https://huggingface.co/FireRedTeam/FireRedVAD
(architecture, CMVN statistics and weights vendored in
backend/am_party/smartfades/analysis/vocal_activity.py and resources/firered_aed*)

Apache License 2.0

Copyright 2026 Xiaohongshu. (Authors: Kaituo Xu, Wenpeng Li, Kai Huang, Kun Liu)

The FireRed AED architecture, fixed CMVN statistics, and model weights are
adapted and redistributed under the Apache License, Version 2.0
(https://www.apache.org/licenses/LICENSE-2.0).

## pywidevine (GPL-3.0)

The container image bundles pywidevine 1.9.0 (https://github.com/devine-dl/pywidevine),
installed unmodified from PyPI as a dependency of the Apple Music provider. pywidevine is
licensed under the GNU General Public License v3.0; its license text ships in the image at
`/opt/venv/lib/python3.14/site-packages/pywidevine-1.9.0.dist-info/LICENSE`, and its
source is available from the project URL above and from PyPI. Distributing the image
(which combines pywidevine with am-party) is subject to the GPL-3.0 terms; the project
owner should take this into account when choosing am-party's own license.
