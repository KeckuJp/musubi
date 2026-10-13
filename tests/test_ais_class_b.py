"""Fixed Class B18 authored reports; no receiver observation claim."""
import csv
import io
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from pyais.encode import encode_dict
from scripts.convert_nmea_wind import convert

ROOT=Path(__file__).resolve().parents[1]

def wire(**extra):
    values=dict(msg_type=18,mmsi=123456789,lon=-70.25,lat=40.5,speed=12.3,
                course=90.,heading=80,second=59,cs=False,reserved_1=17,reserved_2=2,radio=25)
    values.update(extra)
    return encode_dict(values,radio_channel='B')[0]

class ClassBTests(unittest.TestCase):
    def test_two_layouts_reach_common(self):
        reader=os.environ.get('MUSUBI_TELEMETRY_READER');self.assertTrue(reader)
        for text,lon,speed in [(wire(),-70.25,12.3),(wire(mmsi=234567890,lon=135.5,lat=-20.25,speed=0,course=0,heading=0,second=60),135.5,0)]:
            output,report=convert(text,'ais-class-b18')
            row=next(csv.DictReader(io.StringIO(output)))
            self.assertEqual(float(row['ais_longitude_deg']),lon)
            self.assertAlmostEqual(float(row['ais_speed_m_s']),speed*1852/3600)
            self.assertEqual(row['ais_nav_status_code'],'')
            self.assertEqual(row['ais_reserved_1_raw'],'17')
            self.assertEqual(row['ais_reserved_2_raw'],'2')
            self.assertEqual(row['ais_radio_state_raw'],'25')
            self.assertEqual(bytes.fromhex(row['source_sentence_hex'][4:]).decode(),text)
            self.assertEqual(report['ais_records'],1)
            with tempfile.TemporaryDirectory() as d:
                source=Path(d)/'b.csv';source.write_text(output)
                common=json.loads(subprocess.run([reader,str(ROOT/'profiles/declared/nmea-wind/profile.toml'),str(source),'--allow-equal-time'],check=True,capture_output=True).stdout)
                self.assertEqual(common['main_rows'],1)
                self.assertEqual(common['observations'][0]['fields']['ais_longitude_deg'],lon)

    def test_sentinels_selection_and_errors(self):
        row=next(csv.DictReader(io.StringIO(convert(wire(lon=181,lat=91,speed=102.3,course=360,heading=511,second=63),'ais-class-b18')[0])))
        for field in ('ais_longitude_deg','ais_latitude_deg','ais_speed_m_s','ais_course_rad','ais_true_heading_rad'):
            self.assertEqual(row[field],'')
        self.assertEqual(row['ais_second_disposition'],'POSITIONING_SYSTEM_INOPERATIVE_REPORTED')
        row=next(csv.DictReader(io.StringIO(convert(wire(speed=102.2),'ais-class-b18')[0])))
        self.assertEqual(row['ais_speed_disposition'],'AT_LEAST')
        for data in (wire(course=360.1),wire(heading=400),wire(cs=True,second=61),wire()[:-2]+'00'):
            with self.subTest(data=data),self.assertRaises(ValueError):convert(data,'ais-class-b18')
        with self.assertRaises(ValueError):convert(wire(),'ais-class-a')
        with self.assertRaises(ValueError):convert(wire(),'ais-class-b18',static_pairs=True)
        other=wire(msg_type=1)
        output,report=convert(wire()+'\n'+other,'ais-class-b18')
        self.assertEqual(report['unsupported_records'],1)
        old,oldreport=convert(other,'ais-class-a')
        self.assertEqual(oldreport['ais_records'],1)
        self.assertNotIn('ais_class_b_unit',next(csv.DictReader(io.StringIO(old))))


# Type 24 inputs are authored at the bit level: the pinned encoder writes Part A as 168 bits, while
# ITU-R M.1371-5 TABLE 78 fixes it at 160, and it cannot place TABLE 79's EPFD bits.
def armor(bits,talker='AIVDM',channel='B'):
    fill=(-len(bits))%6;bits+='0'*fill
    payload=''.join(chr(v+48 if v<40 else v+56) for v in (int(bits[i:i+6],2) for i in range(0,len(bits),6)))
    body=f'{talker},1,1,,{channel},{payload},{fill}';check=0
    for c in body:check^=ord(c)
    return f'!{body}*{check:02X}'

def six(text,chars):
    return ''.join(format(ord(c)-64 if ord(c)>=64 else ord(c),'06b') for c in text.ljust(chars,'@')[:chars])

def part_a(mmsi,name,repeat=0):
    return armor('011000'+format(repeat,'02b')+format(mmsi,'030b')+'00'+six(name,20))

def part_b(mmsi,ship=0,vendor=None,call='',dims=(0,0,0,0),epfd=0,part=1,cut=0):
    v=six(vendor[0],3)+format(vendor[1],'04b')+format(vendor[2],'020b') if vendor else '0'*42
    d=format(dims[0],'09b')+format(dims[1],'09b')+format(dims[2],'06b')+format(dims[3],'06b')
    bits='011000'+'00'+format(mmsi,'030b')+format(part,'02b')+format(ship,'08b')+v+six(call,7)+d+format(epfd,'04b')+'00'
    return armor(bits[:len(bits)-cut])

class ClassBStaticTests(unittest.TestCase):
    PROFILE=ROOT/'profiles/declared/nmea-wind/profile.toml'

    def rows(self,text,**options):
        output,report=convert(text,'ais-class-b18',class_b_static=True,**options)
        return output,report,list(csv.DictReader(io.StringIO(output)))

    def test_two_layouts_separate_parts_reach_common(self):
        reader=os.environ.get('MUSUBI_TELEMETRY_READER');self.assertTrue(reader)
        layouts=((123456789,'SEA SPRITE',37,('ABC',3,4242),'CALL1',(10,5,2,3),15,'A'),
                 (234567890,'',0,None,'',(0,0,0,0),0,'B'))
        for mmsi,name,ship,vendor,call,dims,epfd,channel in layouts:
            text='\n'.join([wire(mmsi=mmsi),part_a(mmsi,name),part_b(mmsi,ship,vendor,call,dims,epfd)])
            output,report,rows=self.rows(text)
            self.assertEqual([r['ais_message_type'] for r in rows],['18','24','24'])
            self.assertEqual((report['ais_b24_part_a'],report['ais_b24_part_b'],report['unsupported_records']),(1,1,0))
            b18,a,b=rows
            self.assertEqual(b18['ais_b24_part'],'')
            self.assertEqual(b18['ais_vendor_disposition'],'NOT_IN_THIS_MESSAGE')
            self.assertEqual((a['ais_b24_part'],a['ais_ship_name_reported']),('A',name))
            self.assertEqual(a['ais_ship_name_disposition'],'REPORTED' if name else 'NOT_AVAILABLE_OR_BLANK')
            self.assertEqual(a['ais_call_sign_disposition'],'NOT_IN_THIS_PART')
            self.assertEqual(a['ais_position_disposition'],'STATIC_REPORT_CARRIES_NO_POSITION')
            self.assertEqual((b['ais_b24_part'],b['ais_call_sign_reported'],b['ais_ship_type_code']),('B',call,str(ship)))
            self.assertEqual(b['ais_ship_name_disposition'],'NOT_IN_THIS_PART')
            self.assertEqual(b['ais_epfd_code'],str(epfd))
            self.assertEqual([b[f'ais_dimension_{k}_m'] for k in ('bow','stern','port','starboard')],[str(x) for x in dims])
            if vendor:
                self.assertEqual((b['ais_vendor_manufacturer_reported'],b['ais_vendor_model_code'],b['ais_vendor_serial_code']),
                                 ('ABC','3','4242'))
                self.assertEqual(b['ais_epfd_disposition'],'REPORTED_DEFINED_CODE')
            else:
                self.assertEqual(b['ais_vendor_disposition'],'NOT_AVAILABLE_DEFAULT')
                self.assertEqual(b['ais_dimension_disposition'],'DEFAULT_ALL_ZERO_NOT_AVAILABLE')
                self.assertEqual(b['ais_ship_type_disposition'],'NOT_AVAILABLE_OR_NO_SHIP')
                self.assertEqual(b['ais_epfd_disposition'],'UNDEFINED_DEFAULT')
            for row in (a,b):
                self.assertEqual(row['ais_mmsi_reported'],str(mmsi))
                self.assertIn('NOT_JOINED',row['ais_b24_association'])
            with tempfile.TemporaryDirectory() as d:
                source=Path(d)/'b.csv';source.write_text(output)
                common=json.loads(subprocess.run([reader,str(self.PROFILE),str(source),'--allow-equal-time'],
                                                 check=True,capture_output=True).stdout)
                self.assertEqual(common['main_rows'],3)
                self.assertTrue(all(o['clock_basis']=='Unknown' for o in common['observations']))
                self.assertIn('ais_b24_association',common['profile_units'])

    def test_reserved_codes_wrong_shape_and_default_unchanged(self):
        _,_,rows=self.rows(part_b(123456789,150,None,'X',(1,1,1,1),10))
        self.assertEqual((rows[0]['ais_ship_type_disposition'],rows[0]['ais_epfd_disposition']),
                         ('RESERVED_FOR_REGIONAL_USE','NOT_USED_BY_THE_REFERENCE'))
        padded=encode_dict(dict(msg_type=24,mmsi=123456789,partno=0,shipname='PADDED'),radio_channel='B')[0]
        # 168-bit Part A (encoder padding), part number 2, and a validly checksummed 162-bit Part B.
        for bad in (padded,part_b(123456789,part=2),part_b(123456789,cut=6)):
            with self.subTest(bad=bad),self.assertRaises(ValueError):convert(bad,'ais-class-b18',class_b_static=True)
        with self.assertRaises(ValueError):convert(part_a(1,'X'),'ais-class-a',class_b_static=True)
        # A 98XXXYYYY identity: TABLE 79 still defines the 30-bit field as dimensions; the raw field is kept.
        _,_,aux=self.rows(part_b(981234567,dims=(4,3,1,2)))
        self.assertEqual((aux[0]['ais_dimension_bow_m'],aux[0]['ais_b24_dimension_field_raw']),('4',str((4<<21)|(3<<12)|(1<<6)|2)))
        text=wire()+'\n'+part_a(123456789,'SEA SPRITE')
        default,report=convert(text,'ais-class-b18')
        self.assertEqual(report['unsupported_records'],1)
        self.assertEqual(default,convert(wire(),'ais-class-b18')[0])
        self.assertNotIn('ais_b24_part',default.splitlines()[0])

    def test_saved_window_forwards_selector_options(self):
        from scripts.convert_nmea_ascii_window import convert_path
        lines=[wire(mmsi=111111111+i) for i in range(5)]+[part_a(123456789,'SEA SPRITE'),part_b(123456789,37)]
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'capture.nmea';path.write_text('\n'.join(lines)+'\n')
            output,report=convert_path(path,start_record=4,stop_record=7,kind='ais-class-b18',class_b_static=True)
            rows=list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([r['source_record_index'] for r in rows],['4','5','6'])
            self.assertEqual([r['ais_b24_part'] for r in rows],['','A','B'])
            self.assertEqual(report['selector_options']['class_b_static'],True)
            _,plain=convert_path(path,start_record=0,stop_record=2,kind='ais-class-b18')
            self.assertNotIn('selector_options',plain)

if __name__=='__main__':unittest.main()
