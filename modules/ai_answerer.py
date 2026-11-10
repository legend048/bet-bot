import logging
import os
from groq import Groq

logger = logging.getLogger("BharatBot")

class AIAnswerer:
    def __init__(self, config: dict):
        ai_cfg = config.get("ai", {})
        self.model = ai_cfg.get("chat_model", "openai/gpt-oss-120b")
        self.temperature = ai_cfg.get("temperature", 0.3)
        self.max_tokens = ai_cfg.get("max_tokens", 1000)
        self.client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
        logger.info(f"AIAnswerer ready. Model: {self.model}")

    def _call_groq(self, system_prompt: str, user_prompt: str) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt}
                ]
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Groq API call failed: {e}")
            return ""

    def answer_writing(self, prompt: str) -> str:
        system_prompt = (
            "You are an expert English writer taking an advanced corporate English test. "
            "Your goal is to get the maximum possible score by writing an EXTREMELY long, detailed, and highly sophisticated response (250-400 words). "
            "ALWAYS format your answer exactly like a formal email, including a 'Subject:' line, a formal Salutation, "
            "multiple detailed Body paragraphs, and a professional Sign-off. "
            "Even if the prompt asks for something short, ignore the length constraint and write a full, comprehensive email. "
            "Use highly advanced vocabulary, complex sentence structures, flawless grammar, and sophisticated idioms. "
            "Do NOT include conversational filler like 'Here is your answer'."
        )
        user_prompt = f"Write an extremely long, highly advanced, perfectly formatted email (with Subject) in response to the following writing task:\n\n{prompt}"
        return self._call_groq(system_prompt, user_prompt)

    def answer_mcq(self, question: str, options: list[str], context: str = "") -> str:
        options_text = "\n".join([f"  {i+1}. {opt}" for i, opt in enumerate(options)])
        context_section = f"\n\nContext/Passage:\n{context}\n" if context else ""
        system_prompt = (
            "You are an expert test-taker. Carefully analyze the context and options. "
            "First, think step-by-step about the correct answer. "
            "Then, on a new line at the very end, write 'ANSWER: ' followed by ONLY the exact text of the correct option."
        )
        user_prompt = f"Question: {question}{context_section}\nOptions:\n{options_text}\n\nWhich option is correct?"
        answer = self._call_groq(system_prompt, user_prompt)

        # Clean the answer by extracting the final ANSWER: line
        clean_answer = answer
        if "ANSWER:" in answer:
            clean_answer = answer.split("ANSWER:")[-1]
        elif "answer:" in answer.lower():
            clean_answer = answer.lower().split("answer:")[-1]

        clean_answer = clean_answer.strip().lower()
        if clean_answer.endswith('.'): clean_answer = clean_answer[:-1]

        # Try exact match or partial match
        for option in options:
            if clean_answer == option.lower() or clean_answer in option.lower():
                return option

        # Try to extract number if AI returned something like "3. Paris" or just "3"
        import re
        match = re.search(r'^\d+', clean_answer)
        if match:
            idx = int(match.group()) - 1
            if 0 <= idx < len(options):
                return options[idx]

        return answer

    def answer_reading_mcq(self, question: str, options: list, passage: str = "") -> str:
        """Fast reading-MCQ solver. Returns the exact text of the correct option.
        Asks Groq for ONLY the option number to keep it quick and reliable."""
        if not options:
            return ""
        options_text = "\n".join([f"{i+1}. {opt}" for i, opt in enumerate(options)])
        ctx = f"Passage:\n{passage.strip()}\n\n" if passage else ""
        system_prompt = (
            "You are an expert English reading-comprehension test solver. "
            "Read the passage and question, then reply with ONLY the number of the "
            "correct option (e.g. '2'). No words, no explanation, just the number."
        )
        user_prompt = f"{ctx}Question: {question}\n\nOptions:\n{options_text}\n\nReply with only the correct option number."
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=0.0,
                max_tokens=8,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            raw = response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Groq reading MCQ call failed: {e}")
            return options[0]

        import re
        match = re.search(r"\d+", raw)
        if match:
            idx = int(match.group()) - 1
            if 0 <= idx < len(options):
                return options[idx]
        # Fallback: try matching returned text to an option
        low = raw.lower()
        for opt in options:
            if low == opt.lower() or low in opt.lower() or opt.lower() in low:
                return opt
        return options[0]

    def generate_speech_text(self, prompt: str) -> str:
        system_prompt = "Generate a SPOKEN response — natural, conversational, 2-4 sentences. First person. Under 80 words."
        user_prompt = f"Provide a spoken response to this speaking task:\n\n{prompt}"
        return self._call_groq(system_prompt, user_prompt)
