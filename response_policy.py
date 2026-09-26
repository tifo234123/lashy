"""Conservative fast paths: skip research only for clearly local requests."""
import re


def is_greeting(text):
    return bool(re.fullmatch(r'(?:hi|hello|hey|thanks|thank you|good morning|good evening)[!. ]*', text.strip(), re.I))


def needs_web_search(text):
    # Explicit research/current-fact requests always retain web grounding.
    if re.search(r'\b(search|web|internet|latest|current|today|news|price|weather|source|verify|look up|research)\b', text, re.I):
        return True
    if is_greeting(text):
        return False
    if re.fullmatch(r"(?:what(?:'s| is) (?:on|shown on) (?:my|the) screen|(?:describe|read|summarize|explain) (?:this screenshot|this window|this dialog|my screen|the screen)|what does this (?:button|menu|icon) do)[?.! ]*", text.strip(), re.I):
        return False
    if re.fullmatch(r"(?:please )?(?:where is|show me|point (?:at|to)) (?:the )?.{0,70}(?:button|menu|toolbar|text box|checkbox|icon)[?.! ]*", text, re.I):
        return False
    return True  # Ambiguous and complex factual requests keep existing research.


def execution_guidance(text):
    if len(text.split()) <= 12 and not re.search(r'\b(and|then|compare|research|all|every|each)\b', text, re.I):
        return ('This appears to be a small task. Use a concise plan and progress update, '
                'then act and verify. If the screen reveals more work, expand the plan; '
                'never skip requirements or verification for speed.')
    return ('Plan all requirements and preserve constraints. Spend the reasoning and '
            'verification needed to finish the full goal. Report concise progress as you work; '
            'do not repeat the entire plan on every round.')


def settle_delay(response, fallback=1.5):
    from audio.streaming_narration import TAGS
    actions = [m.group() for m in TAGS.finditer(response)
               if re.match(r'\[(EXEC|TYPE|KEY|CLICK|WAIT)(?::|\])', m.group())]
    if not actions:
        return 0.
    if actions[-1].startswith('[WAIT:'):
        return 0.  # The requested wait has already run; don't add another pause.
    if all(a.startswith('[TYPE:') for a in actions):
        return .15  # Typing is synchronous; still take a fresh screenshot next.
    return fallback
