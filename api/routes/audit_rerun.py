"""
Single-page audit re-run.

Split out of api/routes/compare.py (Etapa 5.2 of the consolidation,
docs/CONSOLIDATION_PLAN.md): compare.py bundled three unrelated feature
groups under one router (dashboard charts, ad-hoc audit comparison, and
this single-page re-run). URL path is unchanged
(/api/audits/{audit_id}/rerun/{result_id}) -- this is a file
reorganization, not a behavior change.
"""

import json
import os
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import Audit, AuditResult, get_db
from api.utils.audit_json import AUDIT_ROOT_KEYS
from api.utils.errors import raise_not_found, raise_bad_request

router = APIRouter(prefix="/api", tags=["audit-rerun"])


@router.post("/audits/{audit_id}/rerun/{result_id}")
async def rerun_single_page(
    audit_id: str,
    result_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Re-run analysis for a single page from an existing audit.
    """
    # Get audit
    audit_result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = audit_result.scalar_one_or_none()
    if not audit:
        raise_not_found("Audit")

    if audit.status != "completed":
        raise_bad_request("Can only re-run pages from completed audits")

    # Get the specific result
    result_query = await db.execute(
        select(AuditResult).where(
            AuditResult.audit_id == audit_id,
            AuditResult.id == result_id
        )
    )
    page_result = result_query.scalar_one_or_none()
    if not page_result:
        raise_not_found("Result")

    # Find the corresponding text file
    input_dir = os.path.join(audit.website, "input_llm")
    output_dir = os.path.join(audit.website, f"output_{audit.audit_type.lower()}")

    # The filename in the result might be the output JSON filename
    # We need to find the corresponding .txt input file
    base_name = page_result.filename
    if base_name.endswith('.json'):
        # Strip score prefix and .json extension to find the txt file
        txt_name = re.sub(r'^\d+_', '', base_name).replace('.json', '.txt')
    else:
        txt_name = base_name.replace('.json', '.txt') if not base_name.endswith('.txt') else base_name

    txt_path = os.path.join(input_dir, txt_name)

    if not os.path.exists(txt_path):
        # Try to find it with fuzzy matching
        if os.path.exists(input_dir):
            available = os.listdir(input_dir)
            # Try matching by URL part
            url_part = page_result.page_url.replace('https://', '').replace('http://', '')
            matches = [f for f in available if f.endswith('.txt') and url_part.replace('/', '_') in f]
            if matches:
                txt_path = os.path.join(input_dir, matches[0])
                txt_name = matches[0]
            else:
                raise HTTPException(
                    status_code=404,
                    detail=f"Source text file not found: {txt_name}. Available: {available[:5]}"
                )
        else:
            raise_not_found("Input directory", input_dir)

    # Read the text content
    with open(txt_path, 'r', encoding='utf-8') as f:
        page_text = f.read()

    # Check for research context
    research_dir = os.path.join(audit.website, "research")
    research_context = None
    if os.path.exists(research_dir):
        research_file = os.path.join(research_dir, txt_name.replace('.txt', '.research.json'))
        if os.path.exists(research_file):
            with open(research_file, 'r', encoding='utf-8') as f:
                research_data = json.load(f)
                research_context = "\n\n--- AI SEARCH RESEARCH CONTEXT ---\n"
                for r in research_data.get("results", []):
                    research_context += f"\nQuery: {r.get('query', '')}\n"
                    research_context += f"Response: {r.get('response', '')}\n"
                    research_context += f"Mentions brand: {r.get('mentions_brand', False)}\n"

    if research_context:
        page_text = page_text + research_context

    # Run analysis on single page
    try:
        from core.direct_analyzer import DirectAnalyzer

        analyzer = DirectAnalyzer(
            question_type=audit.audit_type,
            provider=audit.provider.upper(),
            model_name=audit.model,
            max_chars=30000
        )

        # Analyze single page
        result_text = await analyzer.analyze_single_page(page_text, txt_name)

        if result_text:
            # Parse JSON result
            from core.direct_analyzer import clean_json_response
            cleaned = clean_json_response(result_text)
            result_data = json.loads(cleaned)

            # Extract score
            score = None
            # Try all known YAML output_schema root keys
            for key in AUDIT_ROOT_KEYS + ['score', 'overall_score']:
                if key in result_data:
                    val = result_data[key]
                    if isinstance(val, dict):
                        for score_key in ['overall_score', 'score']:
                            if score_key in val:
                                try:
                                    score = int(val[score_key])
                                except (ValueError, TypeError):
                                    continue
                                break
                    elif isinstance(val, (int, float)):
                        score = int(val)
                    elif isinstance(val, str):
                        try:
                            score = int(val)
                        except ValueError:
                            pass
                    if score is not None:
                        break

            # Determine classification
            classification = None
            if score is not None:
                if score >= 85:
                    classification = "excellent"
                elif score >= 70:
                    classification = "good"
                elif score >= 50:
                    classification = "needs_work"
                else:
                    classification = "poor"

            # Capture old score before overwriting
            old_score = page_result.score

            # Update result in database
            page_result.score = score
            page_result.classification = classification
            page_result.result_json = json.dumps(result_data)
            await db.commit()

            # Also save updated JSON file
            if score is not None:
                output_filename = f"{score:03d}_{txt_name.replace('.txt', '.json')}"
            else:
                output_filename = txt_name.replace('.txt', '.json')

            output_path = os.path.join(output_dir, output_filename)

            # Remove old file if it exists
            old_path = os.path.join(output_dir, page_result.filename)
            if os.path.exists(old_path) and old_path != output_path:
                os.remove(old_path)

            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(result_data, f, ensure_ascii=False, indent=2)

            # Update filename in DB
            page_result.filename = output_filename
            await db.commit()

            # Recalculate audit average
            avg_query = select(func.avg(AuditResult.score)).where(
                AuditResult.audit_id == audit_id,
                AuditResult.score.isnot(None)
            )
            avg_result = await db.execute(avg_query)
            new_avg = avg_result.scalar()

            if new_avg is not None:
                audit.average_score = round(new_avg, 1)
                await db.commit()

            return {
                "status": "success",
                "page_url": page_result.page_url,
                "old_score": old_score,
                "new_score": score,
                "new_classification": classification,
                "result_json": result_data
            }
        else:
            raise HTTPException(status_code=500, detail="Analysis returned empty result")

    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse LLM response: {str(e)}")
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Missing dependency: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Re-analysis failed: {str(e)}")
