"""Pure helpers shared by the CAD Map acceptance driver and its tests."""
import copy
from pathlib import Path


def audit_persistence(before,after,output_part_id):
    """Separate one observed ANSA color-display refresh from engineering cards."""
    left,right=copy.deepcopy(before),copy.deepcopy(after);display=[]
    a=next(r for r in left['entities'] if r['entity']['type']=='ANSAPART' and r['entity']['id']==output_part_id)
    b=next(r for r in right['entities'] if r['entity']['type']=='ANSAPART' and r['entity']['id']==output_part_id)
    if a['fields'].get('Is Color Active')=='NO' and b['fields'].get('Is Color Active')=='YES':
        display.append({'type':'ANSAPART','id':output_part_id,'field':'Is Color Active','before':'NO','after':'YES'})
        a['fields']['Is Color Active']=b['fields']['Is Color Active']
    return {'full_snapshot_exact':before==after,'engineering_snapshot_exact':left==right,'display_changes':display}


def resolve_input(path,finalize=False):
    """Validate the explicit input required by new and resumed model runs."""
    if finalize:
        if path is not None:raise ValueError('--finalize-persistence does not accept --input')
        return None
    if path is None:raise ValueError('--input is required for new and resumed CAD Map runs')
    value=Path(path)
    if not value.is_absolute():raise ValueError('--input must be an absolute path')
    if value.suffix.casefold()!='.ansa':raise ValueError('--input must be an .ansa file')
    value=value.resolve()
    if not value.is_file():raise ValueError('--input must be an existing .ansa file')
    return value
