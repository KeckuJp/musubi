"""Authored saved settings; no generated flight path or vehicle operation."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts.convert_saved_mission_plan import convert
from tests.test_saved_mission_plan import plan,item

ROOT=Path(__file__).resolve().parents[1]
POINTS=[[40,20],[40.1,20],[40,20.1]]
def survey():
    return {'type':'ComplexItem','complexItemType':'survey','version':5,'polygon':POINTS,
            'angle':45,'entryLocation':1,'flyAlternateTransects':False,'splitConcavePolygons':True,
            'TransectStyleComplexItem':{'version':2,'TurnAroundDistance':10,'CameraTriggerInTurnAround':True,
             'HoverAndCapture':False,'Refly90Degrees':False,'CameraShots':8,'VisualTransectPoints':POINTS[:2],
             'Items':[{'command':16}], 'CameraCalc':{'version':2,'CameraName':'Manual (no camera specs)',
             'AdjustedFootprintSide':3,'AdjustedFootprintFrontal':5,'DistanceToSurface':40,'DistanceMode':1}}}

class SavedGeometryTests(unittest.TestCase):
    def test_fixed_factory_types_reuse_common_settings(self):
        reader=os.environ.get('MUSUBI_TELEMETRY_READER');self.assertTrue(reader)
        transect=survey()['TransectStyleComplexItem']
        corridor={'type':'ComplexItem','complexItemType':'CorridorScan','version':2,
                  'polyline':POINTS,'CorridorWidth':0,'EntryPoint':3,'TransectStyleComplexItem':transect}
        structure={'type':'ComplexItem','complexItemType':'StructureScan','version':3,
                   'polygon':POINTS,'EntranceAltitude':-2,'ScanBottomAlt':0,'StructureHeight':80,
                   'Layers':4,'GimbalPitch':-45,'StartFromTop':False,'CameraCalc':transect['CameraCalc']}
        landing={'type':'ComplexItem','complexItemType':'fwLandingPattern','version':2,
                 'landingApproachCoordinate':[40,20,30],'landCoordinate':[40.1,20.1,0],
                 'loiterRadius':20,'loiterClockwise':True,'useLoiterToAlt':False,
                 'altitudesAreRelative':True,'stopTakingPhotos':True,'stopVideoPhotos':False,
                 'valueSetIsDistance':True}
        vtol=dict(landing,complexItemType='vtolLandingPattern',version=1,altitudesAreRelative=False)
        vtol.pop('valueSetIsDistance');vtol['landCoordinate']=[41,21,None]
        with tempfile.TemporaryDirectory() as directory:
            for index,items in enumerate(([corridor,structure,landing],[vtol])):
                raw=json.dumps(plan(items=items)).encode()
                output,report=convert(raw,100+index,geometry_settings=True,parameter_meanings=True)
                path=Path(directory)/f'{index}.csv';path.write_text(output)
                result=subprocess.run([reader,str(ROOT/'profiles/declared/saved-mission-plan/profile.toml'),str(path),'--allow-equal-time'],capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr.decode())
                common=json.loads(result.stdout);fields=[x['fields'] for x in common['observations']]
                self.assertEqual(report['complex_items_counted_not_decoded'],[])
                self.assertEqual(report['saved_geometry']['unknown_fields'],[])
                self.assertEqual(len(fields),1+report['saved_geometry']['rows'])
                if index==0:
                    bykey={x['geometry_setting']:x for x in fields if x.get('geometry_setting')}
                    self.assertEqual(bykey['CorridorWidth']['geometry_disposition'],'SINGLE_PASS_DECLARED')
                    self.assertEqual(bykey['EntryPoint']['geometry_disposition'],'OPPOSITE_END_OPPOSITE_SIDE')
                    self.assertEqual(bykey['GimbalPitch']['geometry_value'],-45)
                    self.assertEqual(bykey['EntranceAltitude']['geometry_unit'],'m_home_relative')
                    self.assertEqual(next(x for x in fields if x.get('geometry_kind')=='LANDING_POINT')['geometry_altitude_basis'],'HOME_RELATIVE_DECLARED_NOT_TRANSFORMED')
                else:
                    self.assertEqual(next(x for x in fields if x.get('geometry_kind')=='LANDING_APPROACH')['geometry_altitude_basis'],'AMSL_DECLARED_NOT_TRANSFORMED')
                    self.assertEqual(next(x for x in fields if x.get('geometry_kind')=='LANDING_POINT')['geometry_altitude_basis'],'UNAVAILABLE_NULL')
        for original,key,value in [(corridor,'version',1),(corridor,'EntryPoint',True),
                                   (structure,'Layers',0),(structure,'GimbalPitch',5),
                                   (landing,'altitudesAreRelative',1),(vtol,'landCoordinate',[40,20])]:
            bad=copy.deepcopy(original);bad[key]=value
            with self.assertRaises(ValueError):convert(json.dumps(plan(items=[bad])).encode(),1,geometry_settings=True)

    def test_shapes_settings_and_distinct_empty_unknown_common(self):
        reader=os.environ.get('MUSUBI_TELEMETRY_READER');self.assertTrue(reader)
        first=plan(items=[item(),survey()],geoFence={'version':2,'polygons':[{'version':1,'inclusion':True,'polygon':POINTS}],
                   'circles':[{'version':1,'inclusion':False,'circle':{'center':[41,21],'radius':12}}],
                   'breachReturn':[42,22,30],'future':{'unknown':'retained'}},
                   rallyPoints={'version':2,'points':[[43,23,50],[44,24,None]]})
        second=plan(items=[{'type':'ComplexItem','complexItemType':'corridorScan','version':99}],
                    geoFence={'version':2,'polygons':[],'circles':[]},rallyPoints={'version':2,'points':[]})
        with tempfile.TemporaryDirectory() as directory:
            for index,document in enumerate((first,second)):
                raw=json.dumps(document).encode();output,report=convert(raw,100+index,geometry_settings=True)
                target=Path(directory)/f'{index}.csv';target.write_text(output)
                result=subprocess.run([reader,str(ROOT/'profiles/declared/saved-mission-plan/profile.toml'),str(target),'--allow-equal-time'],capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr.decode())
                common=json.loads(result.stdout);fields=[x['fields'] for x in common['observations']]
                self.assertEqual(common['main_rows'],1+(1 if index==0 else 0)+report['saved_geometry']['rows'])
                if index==0:
                    circle=next(x for x in fields if x.get('geometry_kind')=='FENCE_CIRCLE')
                    self.assertEqual((circle['geometry_radius_m'],circle['geometry_inclusion']),(12,'EXCLUSION_DECLARED'))
                    rally=[x for x in fields if x.get('geometry_kind')=='RALLY_POINT']
                    self.assertEqual(rally[0]['geometry_altitude_basis'],'HOME_RELATIVE_DECLARED_NOT_TRANSFORMED')
                    self.assertEqual(rally[1]['geometry_altitude_basis'],'UNAVAILABLE_NULL')
                    setting=next(x for x in fields if x.get('geometry_setting')=='entryLocation')
                    self.assertEqual(setting['geometry_disposition'],'TOP_RIGHT')
                    flag=next(x for x in fields if x.get('geometry_setting')=='splitConcavePolygons')
                    self.assertEqual(flag['geometry_value'],'true')
                    self.assertEqual(report['saved_geometry']['stored_internal_items_not_expanded'],1)
                    self.assertIn('geoFence/future',report['saved_geometry']['unknown_fields'])
                    self.assertEqual(report['complex_items_counted_not_decoded'],[])
                else:
                    self.assertEqual(len(report['saved_geometry']['uninterpreted_complex_items']),1)
                    self.assertTrue(any(x.get('geometry_kind')=='POINT_COLLECTION' and x['geometry_vertex_count']==0 for x in fields))
                self.assertTrue(all(x['clock_basis']=='Unknown' for x in common['observations']))
            source=Path(directory)/'original.plan';source.write_bytes(json.dumps(first).encode())
            target=Path(directory)/'cli'
            result=subprocess.run([sys.executable,str(ROOT/'scripts/convert_saved_mission_plan.py'),str(source),str(target),'--capture-time-us','100','--geometry-settings'],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            self.assertEqual((target/'original.plan').read_bytes(),source.read_bytes())

    def test_shared_refusals_and_camera_units(self):
        base=plan(items=[survey()],geoFence={'version':2,'polygons':[],'circles':[]},rallyPoints={'version':2,'points':[]})
        for path,value in [(('geoFence','version'),True),(('rallyPoints','points'),None),
                           (('mission','items',0,'polygon'),[[91,20]]),
                           (('mission','items',0,'splitConcavePolygons'),1),
                           (('mission','items',0,'version'),4),
                           (('geoFence','circles'),[{'version':1,'inclusion':False,'circle':{'center':[40,20],'radius':-1}}])]:
            bad=copy.deepcopy(base);node=bad
            for key in path[:-1]:node=node[key]
            node[path[-1]]=value
            with self.assertRaises(ValueError):convert(json.dumps(bad).encode(),1,geometry_settings=True)
        camera=base['mission']['items'][0]['TransectStyleComplexItem']['CameraCalc']
        base['mission']['items'][0]['TransectStyleComplexItem']['TerrainAdjustMaxClimbRate']=0
        camera.update(CameraName='Custom declared',SensorWidth=10,SensorHeight=8,ImageWidth=1000,
                      ImageHeight=800,FocalLength=5,Landscape=True,FixedOrientation=False,
                      MinTriggerInterval=0.5,ValueSetIsDistance=True,ImageDensity=2,FrontalOverlap=60,SideOverlap=70)
        output,report=convert(json.dumps(base).encode(),1,geometry_settings=True)
        self.assertIn('cm/px',output);self.assertIn('SensorWidth',output)
        self.assertIn('NO_MAXIMUM_DECLARED',output)
        self.assertEqual(report['saved_geometry']['unknown_fields'],[])
