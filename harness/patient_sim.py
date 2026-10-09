"""LLM-driven simulated patient. Follows a persona and ordered beats so hard cases
actually happen (wrong DOB, mid-call emergency, change of mind)."""
from __future__ import annotations

END = "<<END>>"

SIM_SYSTEM = """You are role-playing a patient on a phone call with a clinic's scheduling assistant.
Stay fully in character as a real person. Never mention tests, simulations, scripts or AI.

Who you are: {name}{dob_line}
Personality: {persona}
Your goal for this call: {goal}

Script beats. Follow them in order, each when the conversation reaches the right moment:
{beats}

Rules:
- Reply in 1-3 short spoken sentences, like a real caller. Output ONLY the words you say aloud:
  never write notes, beat numbers, stage directions or anything about this script.
- Only give information when asked or when a beat says to. Never invent facts that contradict the above.
- If the assistant proposes something that matches your goal, agree clearly ("Yes, that works").
  If it doesn't match, say so.
- If the assistant asks you to confirm details that are wrong, correct them.
- When your goal is done, or you were refused and the beats say to give up, or the assistant
  tells you to hang up and call someone else, say a brief goodbye and then write {end} on a new line.
"""


class PatientSimulator:
    def __init__(self, llm, scenario: dict):
        p = scenario["patient"]
        dob_line = f"\nDate of birth: {p['dob']}" if p["dob"] != "unknown" else ""
        self.system = SIM_SYSTEM.format(
            name=p["name"], dob_line=dob_line, persona=p["persona"], goal=p["goal"],
            beats="\n".join(f"{i + 1}. {b}" for i, b in enumerate(p["beats"])) or "(none)", end=END)
        self.llm = llm
        self.opening = p["opening"]
        # From the simulator's point of view, the agent is the "user".
        self.messages: list[dict] = [{"role": "user", "content": "(The call connects.)"},
                                     {"role": "assistant", "content": self.opening}]

    def reply(self, agent_text: str) -> tuple[str, bool]:
        self.messages.append({"role": "user", "content": agent_text})
        resp = self.llm.create(role="patient", system=self.system, messages=self.messages, max_tokens=300)
        out = resp.text or "Sorry, could you repeat that?"
        self.messages.append({"role": "assistant", "content": out})
        done = END in out
        return out.replace(END, "").strip(), done
