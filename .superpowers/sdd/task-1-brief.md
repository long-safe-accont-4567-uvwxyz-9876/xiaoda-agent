# Task 1 Brief: 创建 fsrs_model.py 核心算法模块

You are implementing the core FSRS-DSR memory model for a Python agent system. This is a new file with no dependencies on existing code.

## Files to Create/Modify

- **Create:** `memory/fsrs_model.py`
- **Create:** `tests/test_fsrs_model.py`

## Requirements

### memory/fsrs_model.py

Create the FSRS-DSR (Difficulty/Stability/Retrievability) memory model with these exact components:

1. **`MemoryPhase` enum** with values: BUFFER, REINFORCED, DECAY, PERMANENT, ARCHIVED

2. **`ReinforcementSignal` enum** with values: STRONG_CONFIRM, PASSIVE_USE, WEAK_HIT, CORRECT
   - Each must have a `growth_factor` property returning: 2.0, 1.5, 1.0, 0.0 respectively

3. **Constants** (module-level):
   - S_PERMANENT = 30.0
   - R_ARCHIVE = 0.05
   - R_FORGET = 0.02
   - BUFFER_DAYS = 21
   - LOOKBACK_DAYS = 7
   - SIMILARITY_THRESHOLD = 0.6
   - S_INIT = 3.0
   - D_INIT = 5.0
   - D_MEAN = 5.0
   - MEAN_REVERT = 0.3
   - FORGET_THRESHOLD = 0.05
   - DREAM_THRESHOLD = 0.15

4. **`MemoryState` dataclass** with fields:
   - difficulty: float = D_INIT
   - stability: float = S_INIT
   - phase: MemoryPhase = MemoryPhase.BUFFER
   - last_review: float = 0.0
   - created_at: float = 0.0
   - reinforcement_count: int = 0

   Methods:
   - `retrievability(now=None)`: Returns float. BUFFER→1.0, PERMANENT→1.0, ARCHIVED→0.0, else e^(-elapsed_days/stability). `now` defaults to time.time().
   - `transition(now=None)`: Returns MemoryPhase. State machine logic:
     - BUFFER: if age > BUFFER_DAYS → DECAY (if reinforcement_count==0), PERMANENT (if stability>=S_PERMANENT), REINFORCED (otherwise). Else stay BUFFER.
     - REINFORCED/DECAY: if R < R_ARCHIVE → ARCHIVED. if stability >= S_PERMANENT → PERMANENT. Else stay.
     - PERMANENT: stay PERMANENT.

5. **`estimate_initial_difficulty(content, emotion_label="")` function**:
   - Start at D=5.0
   - length < 20: D -= 1.0; length > 200: D += 1.5
   - emotion_label not empty/neutral: D += 1.0
   - fact keywords (生日/电话/地址/名字/日期/号码): D -= 2.0
   - preference keywords (喜欢/讨厌/偏好/习惯/总是): D -= 1.0
   - abstract keywords (因为/所以/意味着/本质上/原理): D += 2.0
   - Clamp to [1.0, 10.0]
   - Note: use elif chain for keyword categories (fact > preference > abstract)

6. **`FSRSModel` class**:
   - `reinforce(state, signal, now=None)`: If signal is CORRECT, call _apply_forget. Otherwise call _apply_recall.
   - `_apply_recall(state, signal, now)`: 
     - R = state.retrievability(now)
     - difficulty_factor = (10-D)/9
     - retrievability_bonus = 1 + 2*(1-R)
     - growth = signal.growth_factor * difficulty_factor * retrievability_bonus
     - S_new = min(S*(1+growth), S*10)
     - Update difficulty via _update_difficulty
     - Update last_review = now, reinforcement_count += 1
     - Check transition, update phase if changed
   - `_apply_forget(state, now)`:
     - R = state.retrievability(now)
     - S_new = S * 0.5 * D^(-0.3) * ((S+1)^0.2 - 1)
     - S_new = max(1.0, S_new)
     - Update difficulty via _update_difficulty with CORRECT signal
     - Update last_review = now
     - Check transition, update phase if changed
   - `_update_difficulty(D, signal)` static method:
     - delta_map: STRONG_CONFIRM→-0.5, PASSIVE_USE→-0.2, WEAK_HIT→0.0, CORRECT→1.0
     - D_new = MEAN_REVERT * D_MEAN + (1-MEAN_REVERT) * (D + delta)
     - Clamp to [1.0, 10.0]
   - `should_filter(R)`: R < FORGET_THRESHOLD
   - `should_archive(R)`: R < DREAM_THRESHOLD
   - `score(similarity, state, now=None)`: similarity * retrievability

### tests/test_fsrs_model.py

Create comprehensive tests covering:
- TestRetrievability: buffer R=1, permanent R=1, archived R=0, decay formula, higher stability = slower decay
- TestTransition: all 6 transition paths (buffer stays, buffer→decay, buffer→reinforced, buffer→permanent, decay→archived, reinforced→permanent)
- TestReinforce: S increases on confirm, S decreases on correct, last_review updates, reinforcement_count increments, low R gives bigger S boost, difficulty decreases on confirm, difficulty increases on correct
- TestEstimateInitialDifficulty: default=5, emotion increases, fact decreases, abstract increases, clamped to [1,10]
- TestFSRSModelScore: score = similarity * R, old memory decays
- TestThresholds: should_filter, should_archive, all constant values

## Testing

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_fsrs_model.py -v`

## Commit

```bash
git add memory/fsrs_model.py tests/test_fsrs_model.py
git commit -m "feat: add FSRS-DSR memory model (Task 1)"
```