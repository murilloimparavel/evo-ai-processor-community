"""Keep conversation-scoped CRM tools out of unbound model requests."""
from copy import deepcopy

CRM_CONVERSATION_TOOLS = frozenset({
    "pipeline_manipulation", "manage_conversation_labels", "transfer_to_human",
})


def has_crm_conversation(state):
    data = state.get("evoai_crm_data") or {}
    if isinstance(data, dict):
        conversation = data.get("conversation") or {}
        if data.get("conversation_id") or (
            isinstance(conversation, dict) and conversation.get("id")
        ):
            return True
    return bool(state.get("conversation_id") or state.get("conversationId"))


async def restrict_unbound_crm_tools(callback_context, llm_request):
    # Only server state authorizes tools. User text, contact IDs and session IDs
    # cannot identify a CRM conversation. Do not mutate the shared agent object.
    if has_crm_conversation(callback_context.state):
        return None
    for name in CRM_CONVERSATION_TOOLS:
        llm_request.tools_dict.pop(name, None)
    config = llm_request.config
    filtered = []
    for original in config.tools or []:
        tool = deepcopy(original)
        declarations = getattr(tool, "function_declarations", None)
        if declarations:
            tool.function_declarations = [
                declaration for declaration in declarations
                if declaration.name not in CRM_CONVERSATION_TOOLS
            ]
            if not tool.function_declarations:
                continue
        filtered.append(tool)
    config.tools = filtered or None
    guard = ("CRM runtime context: this session has no bound CRM conversation. "
             "Conversation label, pipeline and human-transfer tools are unavailable. "
             "Do not call them or claim those actions succeeded. Continue the dialogue "
             "and use reservation lookup when the customer provides its code.")
    instruction = getattr(config, "system_instruction", None)
    if instruction is None or isinstance(instruction, str):
        config.system_instruction = (instruction or "") + "\n\n" + guard
    elif hasattr(instruction, "parts"):
        from google.genai.types import Part
        instruction = deepcopy(instruction)
        instruction.parts = list(instruction.parts or []) + [Part(text=guard)]
        config.system_instruction = instruction
    tool_config = getattr(config, "tool_config", None)
    calling = getattr(tool_config, "function_calling_config", None)
    if calling and calling.allowed_function_names:
        names = [n for n in calling.allowed_function_names
                 if n not in CRM_CONVERSATION_TOOLS]
        calling.allowed_function_names = names or None
        if not names:
            calling.mode = "AUTO"
    return None
