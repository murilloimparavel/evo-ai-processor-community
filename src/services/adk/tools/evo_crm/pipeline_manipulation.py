"""
Pipeline Manipulation Tool

This tool allows agents to manage pipeline items, move contacts through stages,
and create/manage tasks within the pipeline.
"""

from typing import Optional, Dict, Any, List
from uuid import UUID
from google.adk.tools import FunctionTool, ToolContext
from src.services.adk.tools.evo_crm.base import EvoCrmClient
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


async def _get_pipeline_criteria(client, pipeline_id, pipeline_rules):
    """Read only approved criteria for a pipeline explicitly granted to this agent."""
    if not _uuid_identity(pipeline_id) or not any(
        isinstance(rule, dict) and _uuid_identity(rule.get('pipelineId')) == _uuid_identity(pipeline_id)
        for rule in pipeline_rules
    ):
        return {'status': 'error', 'action': 'get_pipeline_criteria', 'message': 'pipeline_not_configured'}
    try:
        response = await client.get(f'/api/v1/pipelines/{pipeline_id}/classification_catalog/projection')
        data = response.get('data') if isinstance(response, dict) else None
        if not isinstance(data, dict) or _uuid_identity(data.get('pipeline_id')) != _uuid_identity(pipeline_id):
            raise ValueError('projection_identity_mismatch')
        if data.get('source') == 'legacy' and data.get('available') is False:
            return {'status': 'success', 'action': 'get_pipeline_criteria', 'criteria_source': 'legacy', 'details': {'available': False}}
        if data.get('source') != 'catalog_v1' or data.get('available') is not True:
            raise ValueError('catalog_unavailable')
        digest, version = data.get('catalog_hash'), data.get('catalog_version')
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('invalid_catalog_version')
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError('invalid_catalog_version')
        purpose, stages = data.get('purpose'), data.get('stages')
        if not isinstance(purpose, str) or len(purpose) > 1200 or not isinstance(stages, list) or len(stages) > 100:
            raise ValueError('invalid_catalog')
        clean_stages, seen = [], set()
        text_bytes = len(purpose.encode('utf-8'))
        for stage in stages:
            if not isinstance(stage, dict) or not _uuid_identity(stage.get('id')) or stage['id'] in seen:
                raise ValueError('invalid_catalog_stage')
            seen.add(stage['id'])
            profile = stage.get('classification_profile')
            if not isinstance(profile, dict) or not isinstance(profile.get('classification_enabled'), bool):
                raise ValueError('invalid_catalog_stage')
            if not profile['classification_enabled']:
                continue
            definition = profile.get('definition')
            if not isinstance(definition, str) or len(definition) > 1200:
                raise ValueError('invalid_catalog_stage')
            clean_profile = {'classification_enabled': profile['classification_enabled'], 'definition': definition}
            for key in ('entry_criteria', 'stay_criteria', 'exit_criteria', 'incompatible_conditions', 'sufficient_examples', 'insufficient_examples'):
                values = profile.get(key, [])
                if not isinstance(values, list) or len(values) > 8 or any(not isinstance(v, str) or len(v) > 1200 for v in values):
                    raise ValueError('invalid_catalog_stage')
                clean_profile[key] = values
            name = stage.get('name')
            if not isinstance(name, str) or len(name) > 200:
                raise ValueError('invalid_catalog_stage')
            text_bytes += len(name.encode('utf-8')) + len(definition.encode('utf-8'))
            text_bytes += sum(len(value.encode('utf-8')) for key in ('entry_criteria', 'stay_criteria', 'exit_criteria', 'incompatible_conditions', 'sufficient_examples', 'insufficient_examples') for value in clean_profile[key])
            if text_bytes > 12 * 1024 or len(clean_stages) >= 20:
                raise ValueError('catalog_context_too_large')
            clean_stages.append({'id': stage['id'], 'name': name, 'classification_profile': clean_profile})
        return {'status': 'success', 'action': 'get_pipeline_criteria', 'criteria_source': 'catalog_v1',
                'details': {'pipeline_id': pipeline_id, 'catalog_hash': digest, 'catalog_version': version,
                            'purpose': purpose, 'stages': clean_stages, 'recommendation_is_financial_proof': False}}
    except Exception:
        return {'status': 'error', 'action': 'get_pipeline_criteria', 'message': 'approved_pipeline_criteria_unavailable'}


def _extract_conversation_id_from_metadata(tool_context: Optional[ToolContext]) -> Optional[str]:
    """Extract conversation_id from tool_context metadata."""
    if not tool_context or not hasattr(tool_context, 'state'):
        return None

    state = tool_context.state
    evoai_crm_data = state.get("evoai_crm_data", {})
    if isinstance(evoai_crm_data, dict):
        conversation_id = evoai_crm_data.get("conversation_id")
        if conversation_id:
            return str(conversation_id)

        conversation = evoai_crm_data.get("conversation", {})
        if isinstance(conversation, dict):
            conv_id = conversation.get("id")
            if conv_id:
                return str(conv_id)

    for key in ["conversation_id", "conversationId"]:
        if key in state:
            return str(state[key])

    return None


def _extract_contact_id_from_metadata(tool_context: Optional[ToolContext]) -> Optional[str]:
    """Extract contact_id from tool_context metadata."""
    if not tool_context or not hasattr(tool_context, 'state'):
        return None

    state = tool_context.state
    contact = state.get("contact")
    if isinstance(contact, dict) and contact.get("id"):
        return str(contact.get("id"))

    evoai_crm_data = state.get("evoai_crm_data", {})
    if isinstance(evoai_crm_data, dict):
        contact_data = evoai_crm_data.get("contact", {})
        if isinstance(contact_data, dict) and contact_data.get("id"):
            return str(contact_data.get("id"))

        contact_id = evoai_crm_data.get("contactId") or evoai_crm_data.get("contact_id")
        if contact_id:
            return str(contact_id)

    contact_id = state.get("contactId") or state.get("contact_id")
    if contact_id:
        return str(contact_id)

    return None


def _extract_pipeline_rules_from_metadata(tool_context: Optional[ToolContext]) -> List[Dict[str, Any]]:
    """Extract pipeline_rules from tool_context metadata."""
    if not tool_context or not hasattr(tool_context, 'state'):
        return []

    state = tool_context.state
    agent_config = state.get("agent_config", {})
    if isinstance(agent_config, dict):
        pipeline_rules = agent_config.get("pipeline_rules", [])
        if pipeline_rules:
            return pipeline_rules if isinstance(pipeline_rules, list) else []

    return []


def _uuid_identity(value: Any) -> Optional[str]:
    """Accept an entire canonical UUID, normalizing only its letter case."""
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        normalized = str(UUID(value))
    except (ValueError, AttributeError):
        return None
    return normalized if value.lower() == normalized else None


def _display_identity(value: Any) -> Optional[str]:
    """Match the CRM's positive PostgreSQL integer display IDs without coercion."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    if not text or not text.isascii() or not text.isdecimal():
        return None
    digits = text.lstrip("0")
    if not digits or len(digits) > 10 or int(digits) > 2147483647:
        return None
    return digits



def _context_conversation_binding(tool_context: Optional[ToolContext]) -> Dict[str, Any]:
    """Bind aliases from CRM metadata, preferring the proven conversation UUID."""
    contextual = _extract_conversation_id_from_metadata(tool_context)
    if contextual is None:
        return {"id": None, "aliases": set(), "valid": True}
    state = tool_context.state
    data = state.get("evoai_crm_data", {})
    data = data if isinstance(data, dict) else {}
    conversation = data.get("conversation", {})
    conversation = conversation if isinstance(conversation, dict) else {}
    identifiers = [data.get("conversation_id"), conversation.get("id"),
                   state.get("conversation_id"), state.get("conversationId")]
    identifiers = [value for value in identifiers if value is not None]
    uuids = {_uuid_identity(value) for value in identifiers if _uuid_identity(value)}
    if len(uuids) > 1:
        return {"valid": False}
    canonical = next(iter(uuids), None)
    aliases = {canonical} if canonical else set()
    if canonical:
        # Only a display number explicitly paired with this UUID is proof of an alias.
        displays = []
        if _uuid_identity(conversation.get("id")) == canonical and "display_id" in conversation:
            displays.append(_display_identity(conversation["display_id"]))
        if _uuid_identity(data.get("conversation_id")) == canonical and "conversation_display_id" in data:
            displays.append(_display_identity(data["conversation_display_id"]))
        if displays and (None in displays or len(set(displays)) != 1):
            return {"valid": False}
        aliases.update(displays)
    else:
        canonical = _display_identity(contextual)
        aliases.add(canonical)
    normalized = [_uuid_identity(value) or _display_identity(value) for value in identifiers]
    valid = canonical is not None and all(value in aliases and value is not None for value in normalized)
    return {"id": canonical, "aliases": aliases, "valid": valid}


def _missing_pipeline_card(error: Exception) -> bool:
    """Read the Rails reason through EvoCrmClient's HTTPError cause chain.

    Legacy CRM versions omit details.reason and use the old specific message.
    Generic 404s, stage/pipeline errors and auth failures never authorize creation.
    """
    response = getattr(error, "response", None)
    if response is None:
        response = getattr(getattr(error, "__cause__", None), "response", None)
    if response is not None:
        if getattr(response, "status_code", None) != 404:
            return False
        try:
            body = response.json()
        except (ValueError, TypeError):
            return False
        details = body.get("error", {}) if isinstance(body, dict) else {}
        if not isinstance(details, dict) or details.get("code") != "RESOURCE_NOT_FOUND":
            return False
        reason = details.get("details", {})
        if isinstance(reason, dict) and "reason" in reason:
            return reason["reason"] == "pipeline_item_not_found"
        message = details.get("message", "")
        return (isinstance(message, str) and "Pipeline conversation " in message
                and "not found in pipeline" in message)
    # Compatibility with legacy client errors that carry only the old message.
    message = str(error)
    return ("HTTP 404" in message and "Pipeline conversation " in message
            and "not found in pipeline" in message)


def _pipeline_identity_error(action: str, reason: str) -> Dict[str, Any]:
    # Never expose the mismatched card, entity details, or raw CRM response.
    logger.warning("Pipeline response identity validation failed: %s", reason)
    return {
        "status": "error",
        "action": action,
        "error": "PIPELINE_IDENTITY_MISMATCH",
        "reason": reason,
        "message": "CRM response did not confirm the requested pipeline target. Stop and verify the operation before retrying.",
    }


def _validate_pipeline_response(
    response: Any,
    action: str,
    pipeline_id: str,
    stage_id: Optional[str],
    conversation_id: Optional[str],
    contact_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Validate the Rails create serializer / move push_event_data contracts.

    This detects a bad acknowledgement after a write; it cannot undo the write.
    The CRM resolver is responsible for preventing mutation of the wrong card.
    Conversation.uuid is a separate survey token, never an alias for its ID.
    """
    def fail(reason: str) -> Dict[str, Any]:
        return _pipeline_identity_error(action, reason)

    if not isinstance(response, dict) or response.get("success") is not True:
        return fail("invalid_success_response")
    item = response.get("data")
    if action == "move_to_stage" and isinstance(item, dict):
        item = item.get("pipeline_item")
    if not isinstance(item, dict) or not _uuid_identity(item.get("id")):
        return fail("missing_pipeline_item")
    expected_pipeline = _uuid_identity(pipeline_id)
    if not expected_pipeline or _uuid_identity(item.get("pipeline_id")) != expected_pipeline:
        return fail("pipeline_mismatch")

    stage_ids = [item[key] for key in ("pipeline_stage_id", "stage_id") if key in item]
    if "pipeline_stage" in item:
        stage = item["pipeline_stage"]
        if not isinstance(stage, dict):
            return fail("stage_mismatch")
        stage_ids.append(stage.get("id"))
        if "pipeline_id" in stage and _uuid_identity(stage["pipeline_id"]) != expected_pipeline:
            return fail("pipeline_mismatch")
    normalized_stages = [_uuid_identity(value) for value in stage_ids]
    if (not normalized_stages or None in normalized_stages
            or len(set(normalized_stages)) != 1):
        return fail("stage_mismatch")
    if stage_id is not None and normalized_stages[0] != _uuid_identity(stage_id):
        return fail("stage_mismatch")

    if conversation_id:
        returned_id = _uuid_identity(item.get("conversation_id"))
        if not returned_id:
            return fail("conversation_mismatch")
        conversation = item.get("conversation", {})
        if not isinstance(conversation, dict):
            return fail("conversation_mismatch")
        if "id" in conversation and _uuid_identity(conversation["id"]) != returned_id:
            return fail("conversation_mismatch")
        expected_uuid = _uuid_identity(conversation_id)
        expected_display = _display_identity(conversation_id)
        if expected_uuid:
            if returned_id != expected_uuid:
                return fail("conversation_mismatch")
        elif not expected_display:
            return fail("conversation_mismatch")
        display_ids = []
        if "conversation_display_id" in item:
            display_ids.append(item["conversation_display_id"])
        if "display_id" in conversation:
            display_ids.append(conversation["display_id"])
        normalized_displays = [_display_identity(value) for value in display_ids]
        if display_ids and (None in normalized_displays
                            or len(set(normalized_displays)) != 1):
            return fail("conversation_mismatch")
        if expected_display and (not normalized_displays
                                 or normalized_displays[0] != expected_display):
            return fail("conversation_mismatch")
        if (item.get("type", "conversation") != "conversation"
                or item.get("is_lead", False) is not False
                or item.get("contact_id") is not None):
            return fail("conversation_mismatch")
        if "item_id" in item and _uuid_identity(item["item_id"]) != returned_id:
            return fail("conversation_mismatch")
    else:
        expected_contact = _uuid_identity(contact_id)
        if not expected_contact or _uuid_identity(item.get("contact_id")) != expected_contact:
            return fail("contact_mismatch")
        if (item.get("conversation_id") is not None
                or item.get("type", "contact") != "contact"):
            return fail("contact_mismatch")
        if item.get("is_lead", True) is not True:
            return fail("contact_mismatch")
        contact = item.get("contact", {})
        if not isinstance(contact, dict) or (
                "id" in contact and _uuid_identity(contact["id"]) != expected_contact):
            return fail("contact_mismatch")
        if "item_id" in item and _uuid_identity(item["item_id"]) != expected_contact:
            return fail("contact_mismatch")
    return None


def create_pipeline_manipulation_tool(
    pipeline_rules: Optional[List[Dict[str, Any]]] = None
) -> FunctionTool:
    """Create the pipeline_manipulation tool for managing pipeline items and tasks.

    This tool allows agents to:
    - Add contacts to pipelines
    - Move contacts between pipeline stages
    - Create tasks for pipeline items
    - Update and complete tasks

    Args:
        pipeline_rules: Optional list of pipeline rules from agent config.
                       Each rule should have: pipelineId, pipelineName, stages, allowTasks, allowServices, etc.
    """

    client = EvoCrmClient()
    default_pipeline_rules = pipeline_rules or []

    async def pipeline_manipulation(
        action: str,
        contact_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        pipeline_id: Optional[str] = None,
        stage_id: Optional[str] = None,
        stage_name: Optional[str] = None,
        notes: Optional[str] = None,
        task_title: Optional[str] = None,
        task_description: Optional[str] = None,
        task_type: Optional[str] = None,
        task_due_date: Optional[str] = None,
        task_priority: Optional[str] = None,
        task_id: Optional[str] = None,
        custom_fields: Optional[Dict[str, Any]] = None,
        tool_context: Optional[ToolContext] = None,
    ) -> Dict[str, Any]:
        """Manage pipeline items, stages, and tasks.

        Use this tool to:
        1. Add contacts to pipelines (action='add_to_pipeline')
        2. Move contacts between stages (action='move_to_stage')
        3. Create tasks for pipeline items (action='create_task')
        4. Update tasks (action='update_task')
        5. Complete tasks (action='complete_task')

        Pipeline Rules:
        If pipeline_rules are configured in the agent, they define which pipelines
        are available and what actions are allowed for each pipeline.

        Each rule has:
        - pipelineId: ID of the pipeline
        - pipelineName: Name of the pipeline
        - stages: List of stages with stageId, stageName, and instructions
        - allowTasks: Whether tasks can be created
        - allowServices: Whether services can be managed
        - generalInstructions: General guidelines for this pipeline

        Args:
            action: The action to perform:
                   - 'add_to_pipeline': Add contact/conversation to a pipeline
                   - 'move_to_stage': Move to a different pipeline stage
                   - 'create_task': Create a new task
                   - 'update_task': Update an existing task
                   - 'complete_task': Mark a task as completed
            contact_id: ID of the contact (optional, auto-extracted from context)
            conversation_id: ID of the conversation (optional, auto-extracted from context)
            pipeline_id: ID of the pipeline (optional if only one pipeline is configured)
            stage_id: ID of the stage to move to (for move_to_stage, use stage_id OR stage_name)
            stage_name: Name of the stage to move to (alternative to stage_id, e.g. "Em Progresso")
            notes: Optional notes for the action
            task_title: Title for the task (required for create_task)
            task_description: Description for the task (optional)
            task_type: Type of task (optional): 'call', 'email', 'meeting', 'follow_up', 'other'
            task_due_date: Due date for the task in ISO format (YYYY-MM-DD) (optional)
            task_priority: Priority of the task (optional): 'low', 'medium', 'high', 'urgent'
            task_id: ID of the task to update/complete (required for update_task and complete_task)
            custom_fields: Custom fields to update (optional)
            tool_context: The tool context containing session information (automatically provided)

        Returns:
            Dictionary with action status and details:
            {
                "status": "success" | "error",
                "message": "Human-readable message",
                "action": "action_performed",
                "details": {...}
            }
        """
        try:
            # Extract IDs from metadata if not provided
            effective_contact_id = contact_id
            if not effective_contact_id and tool_context:
                effective_contact_id = _extract_contact_id_from_metadata(tool_context)
                if effective_contact_id:
                    logger.info(f"Extracted contact_id from metadata: {effective_contact_id}")

            binding = _context_conversation_binding(tool_context)
            if not binding["valid"]:
                return _pipeline_identity_error(action, "context_conversation_mismatch")
            if conversation_id and binding["id"]:
                requested = (_uuid_identity(conversation_id)
                             or _display_identity(conversation_id))
                if requested is None or requested not in binding["aliases"]:
                    return _pipeline_identity_error(action, "context_conversation_mismatch")
            effective_conversation_id = binding["id"] or conversation_id

            # Get pipeline_rules from tool context if not provided during tool creation
            available_pipeline_rules = default_pipeline_rules
            if not available_pipeline_rules and tool_context:
                available_pipeline_rules = _extract_pipeline_rules_from_metadata(tool_context)

            # Determine pipeline_id if not provided
            effective_pipeline_id = pipeline_id
            if not effective_pipeline_id and available_pipeline_rules:
                if len(available_pipeline_rules) == 1:
                    effective_pipeline_id = available_pipeline_rules[0].get("pipelineId")
                    logger.info(f"Using default pipeline from rules: {effective_pipeline_id}")
                elif len(available_pipeline_rules) > 1:
                    return {
                        "status": "error",
                        "message": f"Multiple pipelines configured. Please specify pipeline_id. Available pipelines: {', '.join([r.get('pipelineName', 'Unknown') for r in available_pipeline_rules])}",
                        "action": action,
                    }

            if not effective_pipeline_id:
                return {
                    "status": "error",
                    "message": "pipeline_id is required. No pipeline_rules configured or pipeline_id not provided.",
                    "action": action,
                }

            # Execute the requested action
            if action == 'get_pipeline_criteria':
                return await _get_pipeline_criteria(client, effective_pipeline_id, available_pipeline_rules)

            elif action == 'add_to_pipeline':
                return await _add_to_pipeline(
                    client=client,
                    pipeline_id=effective_pipeline_id,
                    contact_id=effective_contact_id,
                    conversation_id=effective_conversation_id,
                    stage_id=stage_id,
                    custom_fields=custom_fields,
                    pipeline_rules=available_pipeline_rules,
                )

            elif action == 'move_to_stage':
                return await _move_to_stage(
                    client=client,
                    pipeline_id=effective_pipeline_id,
                    conversation_id=effective_conversation_id,
                    stage_id=stage_id,
                    notes=notes,
                    pipeline_rules=available_pipeline_rules,
                    stage_name=stage_name,
                )

            elif action == 'create_task':
                return await _create_task(
                    client=client,
                    pipeline_id=effective_pipeline_id,
                    conversation_id=effective_conversation_id,
                    task_title=task_title,
                    task_description=task_description,
                    task_type=task_type,
                    task_due_date=task_due_date,
                    task_priority=task_priority,
                    pipeline_rules=available_pipeline_rules,
                )

            elif action == 'update_task':
                return await _update_task(
                    client=client,
                    pipeline_id=effective_pipeline_id,
                    task_id=task_id,
                    task_title=task_title,
                    task_description=task_description,
                    task_type=task_type,
                    task_due_date=task_due_date,
                    task_priority=task_priority,
                )

            elif action == 'complete_task':
                return await _complete_task(
                    client=client,
                    pipeline_id=effective_pipeline_id,
                    task_id=task_id,
                )

            else:
                return {
                    "status": "error",
                    "message": f"Invalid action '{action}'. Supported actions: get_pipeline_criteria, add_to_pipeline, move_to_stage, create_task, update_task, complete_task",
                    "action": action,
                }

        except Exception as e:
            error_msg = f"Unexpected error in pipeline manipulation: {str(e)}"
            logger.error(error_msg)
            return {
                "status": "error",
                "message": error_msg,
                "action": action,
                "error": str(e),
            }

    # Set function metadata
    pipeline_manipulation.__name__ = "pipeline_manipulation"

    # Build docstring with pipeline rules information
    pipeline_rules_doc = ""
    if default_pipeline_rules:
        pipeline_rules_doc = "\n\nConfigured Pipeline Rules:\n"
        for i, rule in enumerate(default_pipeline_rules, 1):
            pipeline_name = rule.get("pipelineName", "Unknown")
            allow_tasks = rule.get("allowTasks", False)
            allow_services = rule.get("allowServices", False)
            general_instructions = rule.get("generalInstructions", "")

            pipeline_rules_doc += f"  {i}. Pipeline: {pipeline_name}"
            if allow_tasks:
                pipeline_rules_doc += " (Tasks enabled)"
            if allow_services:
                pipeline_rules_doc += " (Services enabled)"
            if general_instructions:
                pipeline_rules_doc += f"\n     Legacy hints (use only when get_pipeline_criteria explicitly returns criteria_source=legacy): {general_instructions}"

            stages = rule.get("stages", [])
            if stages:
                pipeline_rules_doc += f"\n     Stages:\n"
                for stage in stages:
                    stage_name = stage.get("stageName", "Unknown")
                    stage_instructions = stage.get("instructions", "")
                    pipeline_rules_doc += f"       - {stage_name}"
                    if stage_instructions:
                        pipeline_rules_doc += f": legacy hint: {stage_instructions}"
                    pipeline_rules_doc += "\n"
            pipeline_rules_doc += "\n"

    pipeline_manipulation.__doc__ = f"""Manage pipeline items, stages, and tasks.

    Use this tool to add contacts to pipelines, move them between stages,
    and create/manage tasks for pipeline items.

    Actions:
    - get_pipeline_criteria: Read approved purpose and criteria; call before choosing a stage.
    - add_to_pipeline: Add contact/conversation to a pipeline
    - move_to_stage: Move to a different pipeline stage
    - create_task: Create a new task for a pipeline item
    - update_task: Update an existing task
    - complete_task: Mark a task as completed{pipeline_rules_doc}

    The approved catalog is the sole semantic source when criteria_source=catalog_v1;
    ignore legacy instructions in that case. A dirty/unavailable catalog is an error,
    never permission to fall back. Pipeline rules continue to restrict access/actions.
    Criteria and recommendations never prove payment, reservation, delivery or completion.

    Args:
        action: The action to perform
        contact_id: ID of the contact (optional, auto-extracted)
        conversation_id: ID of the conversation (optional, auto-extracted)
        pipeline_id: ID of the pipeline (optional if only one configured)
        stage_id: ID of the stage (required for move_to_stage)
        notes: Optional notes
        task_title: Title for tasks (required for create_task)
        task_description: Description for tasks
        task_type: Type of task ('call', 'email', 'meeting', 'follow_up', 'other')
        task_due_date: Due date in ISO format (YYYY-MM-DD)
        task_priority: Priority ('low', 'medium', 'high', 'urgent')
        task_id: ID of task to update/complete
        custom_fields: Custom fields to update

    Returns:
        Dictionary with action status and details
    """

    return FunctionTool(func=pipeline_manipulation)


async def _add_to_pipeline(
    client: EvoCrmClient,
    pipeline_id: str,
    contact_id: Optional[str],
    conversation_id: Optional[str],
    stage_id: Optional[str],
    custom_fields: Optional[Dict[str, Any]],
    pipeline_rules: List[Dict[str, Any]],
    _allow_recovery: bool = True,
) -> Dict[str, Any]:
    """Add contact or conversation to a pipeline."""

    # Find pipeline rule
    pipeline_rule = next((r for r in pipeline_rules if r.get("pipelineId") == pipeline_id), None)

    # Determine what to add (contact or conversation)
    if not contact_id and not conversation_id:
        return {
            "status": "error",
            "message": "Either contact_id or conversation_id is required to add to pipeline.",
            "action": "add_to_pipeline",
        }

    # Prepare request body
    request_body: Dict[str, Any] = {}

    if conversation_id:
        request_body["type"] = "conversation"
        request_body["item_id"] = conversation_id
    elif contact_id:
        request_body["type"] = "contact"
        request_body["item_id"] = contact_id

    # Use provided stage_id or get the first stage from rules
    if stage_id:
        request_body["pipeline_stage_id"] = stage_id
    elif pipeline_rule and pipeline_rule.get("stages"):
        first_stage = pipeline_rule["stages"][0]
        request_body["pipeline_stage_id"] = first_stage.get("stageId")
        logger.info(f"Using first stage from pipeline rules: {first_stage.get('stageName')}")

    if custom_fields:
        request_body["custom_fields"] = custom_fields

    logger.info(
        f"Adding {request_body['type']} {request_body['item_id']} to pipeline {pipeline_id}"
    )

    try:
        endpoint = f"/pipelines/{pipeline_id}/pipeline_items"
        response = await client.post(
            endpoint=endpoint,
            json_data=request_body,
        )

        identity_error = _validate_pipeline_response(
            response, "add_to_pipeline", pipeline_id,
            request_body.get("pipeline_stage_id"), conversation_id, contact_id,
        )
        if identity_error:
            return identity_error

        logger.info(f"Successfully added to pipeline {pipeline_id}")

        pipeline_name = pipeline_rule.get("pipelineName", pipeline_id) if pipeline_rule else pipeline_id

        return {
            "status": "success",
            "message": f"Successfully added to pipeline '{pipeline_name}'",
            "action": "add_to_pipeline",
            "pipeline_id": pipeline_id,
            "details": response,
        }

    except Exception as api_error:
        error_message = str(api_error)

        # Creating a card can race the CRM's automatic initial journey.
        # Reuse that active card, only for the exact configured target stage.
        target_stage = request_body.get("pipeline_stage_id")
        configured_stages = {s.get("stageId") for s in (pipeline_rule or {}).get("stages", [])}
        if (_allow_recovery and conversation_id and target_stage
                and target_stage in configured_stages
                and ("HTTP 400" in error_message or "HTTP 409" in error_message)
                and "BUSINESS_RULE_VIOLATION" in error_message
                and "already has an active journey in this pipeline" in error_message):
            return await _move_to_stage(
                client, pipeline_id, conversation_id, target_stage, None,
                pipeline_rules, _allow_recovery=False,
            )

        logger.error("Failed to add to pipeline")
        return {
            "status": "error",
            "message": "CRM could not add the requested pipeline item.",
            "action": "add_to_pipeline",
            "error": "PIPELINE_API_ERROR",
        }


async def _move_to_stage(
    client: EvoCrmClient,
    pipeline_id: str,
    conversation_id: Optional[str],
    stage_id: Optional[str],
    notes: Optional[str],
    pipeline_rules: List[Dict[str, Any]],
    stage_name: Optional[str] = None,
    _allow_recovery: bool = True,
) -> Dict[str, Any]:
    """Move a contact to a different pipeline stage."""

    if not conversation_id:
        return {
            "status": "error",
            "message": "conversation_id is required to move to a different stage.",
            "action": "move_to_stage",
        }

    # Resolve stage_id from stage_name if not provided directly
    if not stage_id and stage_name and pipeline_rules:
        pipeline_rule = next((r for r in pipeline_rules if r.get("pipelineId") == pipeline_id), None)
        if pipeline_rule:
            for s in pipeline_rule.get("stages", []):
                if s.get("stageName", "").lower().strip() == stage_name.lower().strip():
                    stage_id = s.get("stageId")
                    logger.info(f"Resolved stage_id from name '{stage_name}': {stage_id}")
                    break

    if not stage_id:
        # List available stages for the user
        pipeline_rule = next((r for r in pipeline_rules if r.get("pipelineId") == pipeline_id), None)
        available = ", ".join([s.get("stageName", "?") for s in pipeline_rule.get("stages", [])]) if pipeline_rule else "none"
        return {
            "status": "error",
            "message": f"stage_id or stage_name is required. Available stages: {available}",
            "action": "move_to_stage",
        }

    # Find pipeline and stage names from rules
    pipeline_rule = next((r for r in pipeline_rules if r.get("pipelineId") == pipeline_id), None)
    stage_name = "unknown stage"
    if pipeline_rule:
        stage = next((s for s in pipeline_rule.get("stages", []) if s.get("stageId") == stage_id), None)
        if stage:
            stage_name = stage.get("stageName", stage_name)

    logger.info(
        f"Moving conversation {conversation_id} to stage {stage_id} in pipeline {pipeline_id}"
    )

    try:
        endpoint = f"/pipelines/{pipeline_id}/pipeline_items/{conversation_id}/move_to_stage"
        request_body = {
            "new_stage_id": stage_id,
        }

        if notes:
            request_body["notes"] = notes

        response = await client.patch(
            endpoint=endpoint,
            json_data=request_body,
        )

        identity_error = _validate_pipeline_response(
            response, "move_to_stage", pipeline_id, stage_id, conversation_id,
        )
        if identity_error:
            return identity_error

        logger.info(f"Successfully moved to stage {stage_id}")

        return {
            "status": "success",
            "message": f"Successfully moved to stage '{stage_name}'",
            "action": "move_to_stage",
            "pipeline_id": pipeline_id,
            "stage_id": stage_id,
            "stage_name": stage_name,
            "details": response,
        }

    except Exception as api_error:
        # A missing card is different from an invalid stage or unavailable CRM.
        # Create once at the requested configured stage; never retry in a loop.
        configured_stages = {s.get("stageId") for s in (pipeline_rule or {}).get("stages", [])}
        missing_card = _missing_pipeline_card(api_error)
        if _allow_recovery and missing_card and stage_id in configured_stages:
            return await _add_to_pipeline(
                client, pipeline_id, None, conversation_id, stage_id, None,
                pipeline_rules, _allow_recovery=False,
            )

        logger.error("Failed to move to stage")
        return {
            "status": "error",
            "message": "CRM could not move the requested pipeline item.",
            "action": "move_to_stage",
            "error": "PIPELINE_API_ERROR",
        }


async def _create_task(
    client: EvoCrmClient,
    pipeline_id: str,
    conversation_id: Optional[str],
    task_title: Optional[str],
    task_description: Optional[str],
    task_type: Optional[str],
    task_due_date: Optional[str],
    task_priority: Optional[str],
    pipeline_rules: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Create a task for a pipeline item."""

    if not conversation_id:
        return {
            "status": "error",
            "message": "conversation_id is required to create a task.",
            "action": "create_task",
        }

    if not task_title:
        return {
            "status": "error",
            "message": "task_title is required to create a task.",
            "action": "create_task",
        }

    # Check if tasks are allowed for this pipeline
    pipeline_rule = next((r for r in pipeline_rules if r.get("pipelineId") == pipeline_id), None)
    if pipeline_rule and not pipeline_rule.get("allowTasks", False):
        return {
            "status": "error",
            "message": f"Tasks are not enabled for pipeline '{pipeline_rule.get('pipelineName', pipeline_id)}'.",
            "action": "create_task",
        }

    # Get the pipeline_item_id from conversation_id
    # We need to find the pipeline_item that matches this conversation in this pipeline
    try:
        # First, get pipeline items to find the matching one
        items_endpoint = f"/pipelines/{pipeline_id}/pipeline_items"
        items_response = await client.get(
            endpoint=items_endpoint,
        )

        # Find the pipeline item for this conversation
        pipeline_item_id = None
        if isinstance(items_response, dict):
            items = items_response.get("payload", [])
            for item in items:
                if item.get("conversation_id") == conversation_id:
                    pipeline_item_id = item.get("id")
                    break

        if not pipeline_item_id:
            return {
                "status": "error",
                "message": f"Conversation not found in pipeline. Make sure to add the contact to the pipeline first using 'add_to_pipeline'.",
                "action": "create_task",
            }

    except Exception as e:
        logger.error(f"Failed to find pipeline item: {str(e)}")
        return {
            "status": "error",
            "message": f"Failed to find pipeline item for conversation. Error: {str(e)}",
            "action": "create_task",
        }

    logger.info(
        f"Creating task '{task_title}' for pipeline item {pipeline_item_id}"
    )

    try:
        endpoint = f"/pipelines/{pipeline_id}/pipeline_items/{pipeline_item_id}/tasks"
        request_body = {
            "task": {
                "title": task_title,
            }
        }

        if task_description:
            request_body["task"]["description"] = task_description
        if task_type:
            request_body["task"]["task_type"] = task_type
        if task_due_date:
            request_body["task"]["due_date"] = task_due_date
        if task_priority:
            request_body["task"]["priority"] = task_priority

        response = await client.post(
            endpoint=endpoint,
            json_data=request_body,
        )

        logger.info(f"Successfully created task '{task_title}'")

        return {
            "status": "success",
            "message": f"Successfully created task '{task_title}'",
            "action": "create_task",
            "pipeline_id": pipeline_id,
            "task_title": task_title,
            "details": response,
        }

    except Exception as api_error:
        error_message = str(api_error)

        if "404" in error_message or "not found" in error_message.lower():
            error_message = f"Pipeline item not found. Make sure the contact is in the pipeline."

        logger.error(f"Failed to create task: {error_message}")

        return {
            "status": "error",
            "message": error_message,
            "action": "create_task",
            "error": str(api_error),
        }


async def _update_task(
    client: EvoCrmClient,
    pipeline_id: str,
    task_id: Optional[str],
    task_title: Optional[str],
    task_description: Optional[str],
    task_type: Optional[str],
    task_due_date: Optional[str],
    task_priority: Optional[str],
) -> Dict[str, Any]:
    """Update an existing task."""

    if not task_id:
        return {
            "status": "error",
            "message": "task_id is required to update a task.",
            "action": "update_task",
        }

    logger.info(f"Updating task {task_id}")

    try:
        endpoint = f"/pipelines/{pipeline_id}/tasks/{task_id}"
        request_body = {"task": {}}

        if task_title:
            request_body["task"]["title"] = task_title
        if task_description:
            request_body["task"]["description"] = task_description
        if task_type:
            request_body["task"]["task_type"] = task_type
        if task_due_date:
            request_body["task"]["due_date"] = task_due_date
        if task_priority:
            request_body["task"]["priority"] = task_priority

        if not request_body["task"]:
            return {
                "status": "error",
                "message": "At least one field must be provided to update the task.",
                "action": "update_task",
            }

        response = await client.put(
            endpoint=endpoint,
            json_data=request_body,
        )

        logger.info(f"Successfully updated task {task_id}")

        return {
            "status": "success",
            "message": f"Successfully updated task",
            "action": "update_task",
            "task_id": task_id,
            "details": response,
        }

    except Exception as api_error:
        error_message = str(api_error)

        if "404" in error_message or "not found" in error_message.lower():
            error_message = f"Task not found."

        logger.error(f"Failed to update task: {error_message}")

        return {
            "status": "error",
            "message": error_message,
            "action": "update_task",
            "error": str(api_error),
        }


async def _complete_task(
    client: EvoCrmClient,
    pipeline_id: str,
    task_id: Optional[str],
) -> Dict[str, Any]:
    """Mark a task as completed."""

    if not task_id:
        return {
            "status": "error",
            "message": "task_id is required to complete a task.",
            "action": "complete_task",
        }

    logger.info(f"Completing task {task_id}")

    try:
        endpoint = f"/pipelines/{pipeline_id}/tasks/{task_id}/complete"

        response = await client.post(
            endpoint=endpoint,
            json_data={},
        )

        logger.info(f"Successfully completed task {task_id}")

        return {
            "status": "success",
            "message": f"Successfully completed task",
            "action": "complete_task",
            "task_id": task_id,
            "details": response,
        }

    except Exception as api_error:
        error_message = str(api_error)

        if "404" in error_message or "not found" in error_message.lower():
            error_message = f"Task not found."

        logger.error(f"Failed to complete task: {error_message}")

        return {
            "status": "error",
            "message": error_message,
            "action": "complete_task",
            "error": str(api_error),
        }
