"""Read fixed QGC saved geometry/settings; never synthesize mission items."""
PIN = 'b809674075f9c12ee4830a5ca824308d249b7ae4'
COMPLEX_VERSIONS = {'survey':5, 'CorridorScan':2, 'StructureScan':3,
                    'fwLandingPattern':2, 'vtolLandingPattern':1}
CORRIDOR = {'CorridorWidth':('number','m'), 'EntryPoint':('corridor_entry','declared_entry')}
STRUCTURE = {'EntranceAltitude':('signed','m_home_relative'), 'ScanBottomAlt':('signed','m_home_relative'),
             'StructureHeight':('number','m_home_relative_structure_top'), 'Layers':('integer','declared_layers'),
             'GimbalPitch':('signed','gimbal-deg'), 'StartFromTop':('bool','declared_flag')}
LANDING = {'loiterRadius':('number','m'), 'loiterClockwise':('bool','declared_flag'),
           'useLoiterToAlt':('bool','declared_flag'), 'altitudesAreRelative':('bool','declared_flag'),
           'stopTakingPhotos':('bool','declared_flag'), 'stopVideoPhotos':('bool','declared_flag')}
FIELDS = ['geometry_source_pin', 'geometry_owner', 'geometry_kind', 'geometry_index',
          'geometry_vertex_index', 'geometry_vertex_count', 'geometry_inclusion',
          'geometry_latitude_deg', 'geometry_longitude_deg', 'geometry_altitude_m',
          'geometry_altitude_basis', 'geometry_radius_m', 'geometry_setting',
          'geometry_value', 'geometry_value_type', 'geometry_unit', 'geometry_disposition']
SURVEY = {'angle':('number','deg'), 'entryLocation':('entry','declared_corner'),
          'flyAlternateTransects':('bool','declared_flag'), 'splitConcavePolygons':('bool','declared_flag')}
TRANSECT = {'TurnAroundDistance':('number','m'), 'CameraTriggerInTurnAround':('bool','declared_flag'),
            'HoverAndCapture':('bool','declared_flag'), 'Refly90Degrees':('bool','declared_flag'),
            'CameraShots':('integer','reported_count')}
TERRAIN = {'TerrainAdjustTolerance':('number','m'), 'TerrainAdjustMaxClimbRate':('number','m/s'),
           'TerrainAdjustMaxDescentRate':('number','m/s'), 'TerrainFlightSpeed':('number','m/s')}
CAMERA = {'AdjustedFootprintSide':('number','m'), 'AdjustedFootprintFrontal':('number','m'),
          'DistanceToSurface':('number','m_per_declared_DistanceMode'),
          'DistanceMode':('altmode','declared_altitude_mode'), 'CameraName':('text','reported_name')}
SPEC = {'SensorWidth':('number','mm'), 'SensorHeight':('number','mm'),
        'ImageWidth':('integer','px'), 'ImageHeight':('integer','px'),
        'FocalLength':('number','mm'), 'Landscape':('bool','declared_flag'),
        'FixedOrientation':('bool','declared_flag'), 'MinTriggerInterval':('number','s'),
        'ValueSetIsDistance':('bool','declared_flag'), 'ImageDensity':('number','cm/px'),
        'FrontalOverlap':('number','%'), 'SideOverlap':('number','%')}

def read(document, number, whole):
    """Yield rows and mutate finite accounting; raw source must accompany output."""
    report = {'source_pin':PIN, 'unknown_fields':[], 'uninterpreted_complex_items':[],
              'stored_internal_items_not_expanded':0, 'rows':0, 'sections':{}}

    def obj(value):
        if type(value) is not dict: raise ValueError('saved geometry object required')
        return value
    def seq(value):
        if type(value) is not list: raise ValueError('saved geometry list required')
        return value
    def version(value, expected):
        if type(value.get('version')) is not int or value['version'] != expected:
            raise ValueError('unqualified saved geometry version')
    def unknown(value, known, path):
        report['unknown_fields'] += [path+'/'+key for key in sorted(set(value)-set(known))]
    def row(owner, kind, index='', **extra):
        result = dict.fromkeys(FIELDS, '')
        result.update(geometry_source_pin=PIN, geometry_owner=owner, geometry_kind=kind,
                      geometry_index=index, geometry_disposition='SAVED_DECLARATION_NOT_EXECUTION')
        result.update(extra);report['rows']+=1
        return result
    def coordinate(value, altitude):
        if type(value) is not list or len(value) != (3 if altitude else 2):
            raise ValueError('exact saved coordinate dimensions required')
        result = dict(geometry_latitude_deg=number(value[0],'latitude',-90,90),
                      geometry_longitude_deg=number(value[1],'longitude',-180,180))
        if altitude:
            if value[2] is None:
                result.update(geometry_altitude_basis='UNAVAILABLE_NULL',geometry_altitude_m='')
            else:
                result.update(geometry_altitude_m=number(value[2],'relative altitude'),
                              geometry_altitude_basis='HOME_RELATIVE_DECLARED_NOT_TRANSFORMED')
        return result
    def shape(owner, kind, index, points, inclusion=''):
        points=seq(points)
        yield row(owner,kind,index,geometry_vertex_count=len(points),geometry_inclusion=inclusion)
        for i,point in enumerate(points):
            yield row(owner,kind+'_VERTEX',index,geometry_vertex_index=i,geometry_inclusion=inclusion,
                      **coordinate(point,False))
    def settings(owner, value, table):
        for key,(kind,unit) in table.items():
            if key not in value: raise ValueError('missing required saved setting '+key)
            v=value[key];disposition='DECLARED';output=''
            if v is None and kind in ('number','signed'): disposition='UNAVAILABLE_NULL'
            elif kind=='bool':
                if type(v) is not bool: raise ValueError('saved setting requires boolean')
                output='true' if v else 'false'
            elif kind=='text':
                if type(v) is not str or not v:raise ValueError('saved setting requires text')
                output='hex:'+v.encode().hex()
            elif kind in ('integer','entry','altmode','corridor_entry'):
                if type(v) is bool:raise ValueError('integer setting required')
                text=number(v,key)
                from decimal import Decimal
                parsed=Decimal(text)
                if parsed != parsed.to_integral_value() or not 0 <= parsed <= 2**53-1:
                    raise ValueError('exact nonnegative integer setting required')
                output=text
                if kind=='entry':
                    disposition={0:'TOP_LEFT',1:'TOP_RIGHT',2:'BOTTOM_LEFT',3:'BOTTOM_RIGHT'}.get(int(parsed),'UNKNOWN_ENUM_RETAINED')
                if kind=='altmode':
                    disposition={0:'MIXED',1:'HOME_RELATIVE',2:'AMSL',3:'CALCULATED_ABOVE_TERRAIN',4:'TERRAIN_FRAME',5:'DISTANCE_NOT_ALTITUDE'}.get(int(parsed),'UNKNOWN_ENUM_RETAINED')
                if kind=='corridor_entry':
                    disposition={0:'DEFAULT_END_DEFAULT_SIDE',1:'DEFAULT_END_OPPOSITE_SIDE',2:'OPPOSITE_END_DEFAULT_SIDE',3:'OPPOSITE_END_OPPOSITE_SIDE'}.get(int(parsed),'UNKNOWN_ENUM_RETAINED')
                if key=='Layers' and int(parsed)<1:raise ValueError('positive layer count required')
            else:
                output=number(v,key)
                if key=='angle':number(v,key,-360,360)
                elif kind!='signed' and float(output)<0:raise ValueError('negative geometric setting')
                if key=='GimbalPitch':number(v,key,-90,0)
                if key in ('TerrainAdjustMaxClimbRate','TerrainAdjustMaxDescentRate') and float(output)==0:
                    disposition='NO_MAXIMUM_DECLARED'
                if key=='CorridorWidth' and float(output)==0:disposition='SINGLE_PASS_DECLARED'
            yield row(owner,'SETTING',geometry_setting=key,geometry_value=output,
                      geometry_value_type=kind,geometry_unit=unit,geometry_disposition=disposition)

    def camera_settings(owner, value):
        camera=obj(value);version(camera,2)
        table=dict(CAMERA)
        if camera.get('CameraName')!='Manual (no camera specs)':table.update(SPEC)
        unknown(camera,('version',*table),owner+'/CameraCalc')
        yield from settings(owner,camera,table)

    def generate():
        for section in ('geoFence','rallyPoints'):
            report['sections'][section] = 'PRESENT' if section in document else 'ABSENT'
            if section not in document:continue
            value=obj(document[section]);version(value,2)
            yield row(section,'SECTION',geometry_disposition='PRESENT_DECLARED_SECTION')
            if section=='rallyPoints':
                points=seq(value.get('points'));unknown(value,('version','points'),section)
                yield row(section,'POINT_COLLECTION',geometry_vertex_count=len(points))
                for i,point in enumerate(points):yield row(section,'RALLY_POINT',i,**coordinate(point,True))
                continue
            unknown(value,('version','polygons','circles','breachReturn'),section)
            for collection in ('polygons','circles'):
                shapes=seq(value.get(collection))
                yield row(section,collection.upper()+'_COLLECTION',geometry_vertex_count=len(shapes))
                for i,saved in enumerate(shapes):
                    saved=obj(saved);version(saved,1)
                    if type(saved.get('inclusion')) is not bool:raise ValueError('explicit inclusion flag required')
                    inclusion='INCLUSION_DECLARED' if saved['inclusion'] else 'EXCLUSION_DECLARED'
                    unknown(saved,('version','inclusion','polygon' if collection=='polygons' else 'circle'),f'{section}/{collection}/{i}')
                    if collection=='polygons':
                        yield from shape(section,'FENCE_POLYGON',i,saved.get('polygon'),inclusion)
                    else:
                        circle=obj(saved.get('circle'));unknown(circle,('center','radius'),f'{section}/circles/{i}/circle')
                        radius=number(circle.get('radius'),'radius')
                        if float(radius)<=0:raise ValueError('positive circle radius required')
                        yield row(section,'FENCE_CIRCLE',i,geometry_radius_m=radius,
                                  geometry_inclusion=inclusion,**coordinate(circle.get('center'),False))
            if 'breachReturn' in value:yield row(section,'BREACH_RETURN',**coordinate(value['breachReturn'],True))
        for i,saved in enumerate(document['mission']['items']):
            if saved.get('type')!='ComplexItem':continue
            owner=f'mission/items/{i}';kind=saved.get('complexItemType')
            if kind not in COMPLEX_VERSIONS:
                report['uninterpreted_complex_items'].append({'item_index':i,'type':kind})
                yield row(owner,'UNINTERPRETED_COMPLEX_ITEM',i,geometry_disposition='UNQUALIFIED_TYPE_RAW_SOURCE_RETAINED')
                continue
            version(saved,COMPLEX_VERSIONS[kind])
            if kind in ('fwLandingPattern','vtolLandingPattern'):
                table=dict(LANDING)
                if kind=='fwLandingPattern':table['valueSetIsDistance']=('bool','declared_flag')
                unknown(saved,('type','complexItemType','version','landingApproachCoordinate','landCoordinate',*table),owner)
                yield from settings(owner,saved,table)
                for key,label in [('landingApproachCoordinate','LANDING_APPROACH'),('landCoordinate','LANDING_POINT')]:
                    point=coordinate(saved.get(key),True)
                    if point['geometry_altitude_m']!='':
                        point['geometry_altitude_basis']=('HOME_RELATIVE_DECLARED_NOT_TRANSFORMED' if saved['altitudesAreRelative'] else 'AMSL_DECLARED_NOT_TRANSFORMED')
                    yield row(owner,label,i,**point)
                continue
            if kind=='StructureScan':
                unknown(saved,('type','complexItemType','version','polygon','CameraCalc',*STRUCTURE),owner)
                yield from shape(owner,'STRUCTURE_POLYGON',i,saved.get('polygon'))
                yield from settings(owner,saved,STRUCTURE)
                yield from camera_settings(owner,saved.get('CameraCalc'))
                continue
            table=SURVEY if kind=='survey' else CORRIDOR
            shape_key='polygon' if kind=='survey' else 'polyline'
            unknown(saved,('type','complexItemType','version',shape_key,'TransectStyleComplexItem',*table),owner)
            yield from shape(owner,'SURVEY_POLYGON' if kind=='survey' else 'CORRIDOR_POLYLINE',i,saved.get(shape_key))
            yield from settings(owner,saved,table)
            transect=obj(saved.get('TransectStyleComplexItem'));version(transect,2)
            unknown(transect,('version','CameraCalc','VisualTransectPoints','Items',*TRANSECT,*TERRAIN),owner+'/TransectStyleComplexItem')
            yield from settings(owner,transect,TRANSECT)
            terrain={key:spec for key,spec in TERRAIN.items() if key in transect}
            yield from settings(owner,transect,terrain)
            yield from shape(owner,'SAVED_VISUAL_TRANSECT',i,transect.get('VisualTransectPoints'))
            items=seq(transect.get('Items'))
            report['stored_internal_items_not_expanded']+=len(items)
            yield row(owner,'STORED_INTERNAL_ITEMS',geometry_vertex_count=len(items),
                      geometry_disposition='RETAINED_IN_ORIGINAL_NOT_GENERATED_OR_INTERPRETED')
            yield from camera_settings(owner,transect.get('CameraCalc'))
    return generate(),report
