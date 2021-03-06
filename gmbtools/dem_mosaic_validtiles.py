#! /usr/bin/env python
"""
Run dem_mosaic in parallel for valid tiles only
"""

import os
import sys
import glob
import argparse
import math
import time
import subprocess
import tarfile
import pickle
import copy
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from osgeo import gdal, ogr, osr

from pygeotools.lib import geolib, warplib, iolib

from dem_mosaic_index_ts import make_dem_mosaic_index_ts

#Hack to work around file open limit
#Set this in shell with `ulimit -n 65536` before running
#import resource
#resource.setrlimit(resource.RLIMIT_NOFILE,(resource.RLIM_INFINITY, resource.RLIM_INFINITY))

def getparser():
    stat_choices = ['first', 'firstindex', 'last', 'lastindex', 'min', 'max', 'mean', 'stddev', 'count', 'median', 'medianindex', 'nmad', 'wmean']
    parser = argparse.ArgumentParser(description='Wrapper for dem_mosaic that will only write valid tiles')
    parser.add_argument('--tr', default='min', help='Output resolution (default: %(default)s)')
    parser.add_argument('--t_projwin', default='union', help='Output extent (default: %(default)s)')
    parser.add_argument('--t_srs', default='first', help='Output projection (default: %(default)s)')
    parser.add_argument('--georef_tile_size', type=float, default=100000., help='Output tile width (meters)')
    parser.add_argument('--threads', type=int, default=iolib.cpu_count(logical=False), help='Number of simultaneous dem_mosaic processes to run')
    parser.add_argument('--stat', type=str, nargs='*', default=None, choices=stat_choices, \
            help='Specify space-delimited list of output statistics to pass to dem_mosaic (e.g., "count stddev", default: wmean)')
    parser.add_argument('-o', type=str, default=None, help='Output mosaic prefix')
    #parser.add_argument('-i', type=str, default=None, help='Input file list (e.g., fn_list.txt)')
    parser.add_argument('src_fn_list', type=str, nargs='+', help='Input filenames (img1.tif img2.tif ...)')
    return parser

def main():
    parser = getparser()
    args = parser.parse_args()

    stat_list = ['wmean',]
    if args.stat is not None:
        if isinstance(args.stat, str):
            stat_list = args.stat.split()
        else:
            stat_list = args.stat

    print("The following mosaics will be generated:")
    print(stat_list)

    #Tile dimensions in output projected units (meters)
    #Assume square
    tile_width = args.georef_tile_size
    tile_height = tile_width

    #This is number of simultaneous processes, each with one thread
    threads = args.threads

    #Might hit OS open file limit here
    #Workaround is to provide input filelist
    if len(args.src_fn_list) == 1 and os.path.splitext(args.src_fn_list[0])[-1] == '.txt':
        print("Reading filenames from input text file")
        with open(args.src_fn_list[0]) as f:
            fn_list = f.read().splitlines()
    else:
        fn_list = args.src_fn_list

    #Sort?

    #Create output directory
    o = args.o
    if o is None:
        #o = 'mos_%im/mos' % tr
        o = 'mos/mos' % tr
    odir = os.path.dirname(o)
    #If dirname is empty, use prefix for new directory
    if not odir:
        odir = o
        o = os.path.join(odir, o)
    if not os.path.exists(odir): 
        os.makedirs(odir)
    iolib.setstripe(odir, threads)

    out_pickle_fn = o+'_tile_dict.pkl'
    if os.path.exists(out_pickle_fn):
        print("Loading existing tile dictionary")
        with open(out_pickle_fn, 'rb') as f:
            tile_dict = pickle.load(f)
        dummy = list(tile_dict.values())[0]
        tr = dummy['tr']
        t_srs = osr.SpatialReference()
        t_srs.ImportFromProj4(dummy['t_srs'])
        t_projwin = dummy['t_projwin']
    else:
        print("Loading input datasets")
        print("Note: this could take several minutes depending on number of inputs and I/O performance")
        ds_list = []
        for n, fn in enumerate(fn_list):
            if (n % 100 == 0):
                print('%i of %i done' % (n, len(fn_list)))
            ds_list.append(gdal.Open(fn))

        #Mosaic t_srs
        print("\nParsing t_srs")
        t_srs = warplib.parse_srs(args.t_srs, ds_list)
        print(t_srs.ExportToProj4())
        #Output file names will contain coordinate string
        latlon = False
        if t_srs.IsGeographic():
            latlon = True

        #Mosaic res
        print("\nParsing tr")
        tr = warplib.parse_res(args.tr, ds_list, t_srs=t_srs) 
        print(tr)

        #Mosaic extent 
        #xmin, ymin, xmax, ymax
        print("Determining t_projwin (bounding box for inputs)")
        t_projwin = warplib.parse_extent(args.t_projwin, ds_list, t_srs=t_srs) 
        print(t_projwin)
        #Ensure that our extent is whole integer multiples of the mosaic res
        #This could trim off some fraction of a pixel around margins
        t_projwin = geolib.extent_round(t_projwin, tr)
        mos_xmin, mos_ymin, mos_xmax, mos_ymax = t_projwin

        #Compute extent geom for all input datsets
        print("Computing extent geom for all input datasets")
        input_geom_dict = OrderedDict()
        for n, ds in enumerate(ds_list):
            if (n % 100 == 0):
                print('%i of %i done' % (n, len(ds_list)))
            ds_geom = geolib.ds_geom(ds, t_srs)
            ds_fn = ds.GetFileList()[0]
            #Could use filename as key here
            input_geom_dict[ds_fn] = geolib.geom_dup(ds_geom)
            ds = None

        ds_list = None

        #Mosaic tile size
        #Should have float extent and tile dim here
        ntiles_w = int(math.ceil((mos_xmax - mos_xmin)/tile_width))
        ntiles_h = int(math.ceil((mos_ymax - mos_ymin)/tile_height))
        ntiles = ntiles_w * ntiles_h
        print("%i (%i cols x %i rows) tiles required for full mosaic" % (ntiles, ntiles_w, ntiles_h))
        #Use this for zero-padding of tile number
        ntiles_digits = len(str(ntiles))

        print("Computing extent geom for all output tiles")
        tile_dict = OrderedDict()
        for i in range(ntiles_w):
            for j in range(ntiles_h):
                tilenum = j*ntiles_w + i
                tile_xmin = mos_xmin + i*tile_width
                tile_xmax = mos_xmin + (i+1)*tile_width
                tile_ymax = mos_ymax - j*tile_height
                tile_ymin = mos_ymax - (j+1)*tile_height
                #Corner coord needed for geom
                x = [tile_xmin, tile_xmax, tile_xmax, tile_xmin, tile_xmin]
                y = [tile_ymax, tile_ymax, tile_ymin, tile_ymin, tile_ymax]
                tile_geom_wkt = 'POLYGON(({0}))'.format(', '.join(['{0} {1}'.format(*a) for a in zip(x,y)]))
                tile_geom = ogr.CreateGeometryFromWkt(tile_geom_wkt)
                tile_geom.AssignSpatialReference(t_srs)
                #tile_dict[tilenum] = tile_geom
                tile_dict[tilenum] = {}
                tile_dict[tilenum]['geom'] = tile_geom
                tile_dict[tilenum]['extent'] = [tile_xmin, tile_ymin, tile_xmax, tile_ymax]
                #Add center coord tile name
                cx = tile_geom.Centroid().GetX()
                cy = tile_geom.Centroid().GetY()
                #These round down
                #TanDEM-X uses lower left corner as name
                if latlon:
                    tilename = '{:.0f}N'.format(cy) + '{:03.0f}E'.format(cx)
                else:
                    tilename = '{:.0f}'.format(cy) + '_' + '{:.0f}'.format(cx) 
                tile_dict[tilenum]['tilename'] = tilename

                #Add additional parameters that can be loaded at a later time without reprocessing all input datasets
                tile_dict[tilenum]['tr'] = tr
                tile_dict[tilenum]['t_srs'] = t_srs.ExportToProj4() 
                #This is full extent, but preserve here
                tile_dict[tilenum]['t_projwin'] = t_projwin

        print("Computing valid intersections between input dataset geom and tile geom")
        for tilenum in sorted(tile_dict.keys()):
            print('%i of %i' % (tilenum, len(tile_dict.keys())))
            tile_geom = tile_dict[tilenum]['geom']
            tile_dict_fn = []
            for ds_fn, ds_geom in input_geom_dict.items():
                if tile_geom.Intersects(ds_geom):
                    tile_dict_fn.append(ds_fn)
                    #Write out shp for debugging
                    #geolib.geom2shp(tile_geom, 'tile_%03i.shp' % tilenum)
            if tile_dict_fn:
                tile_dict[tilenum]['fn_list'] = tile_dict_fn
       
        #This needs to be cleaned up, just create a new tile_dict, don't need list
        out_tile_list = []
        tile_dict_copy = copy.deepcopy(tile_dict)
        for tilenum in tile_dict_copy.keys():
            if 'fn_list' in tile_dict[tilenum]:
                out_tile_list.append(tilenum)
            else:
                del tile_dict[tilenum]

        print("%i valid output tiles" % len(out_tile_list))
        out_tile_list.sort()
        out_tile_list = list(set(out_tile_list))
        out_tile_list_str = ' '.join(map(str, out_tile_list))
        print(out_tile_list_str)

        #Write out dictionary with list of fn for each tile
        print("Writing out tile dictionary")
        with open(out_pickle_fn, 'wb') as f:
            pickle.dump(tile_dict, f)

    delay = 0.001
    outf = open(os.devnull, 'w') 
    #outf = open('%s-log-dem_mosaic-tile-%i.log' % (o, tile), 'w')

    #Should run the tiles with the largest file count first, as they will likely take longer
    tile_dict = OrderedDict(sorted(tile_dict.items(), key=lambda item: len(item[1]['fn_list']), reverse=True))
    #Do tiles with smallest file count first
    #tile_dict = OrderedDict(sorted(tile_dict.items(), key=lambda item: len(item[1]['fn_list']), reverse=False))
    out_tile_list = tile_dict.keys()
    #Number of integers to use for tile number
    ni = max([len(str(i)) for i in out_tile_list])

    #If we're on Pleiades, split across multiple nodes
    #Hack with GNU parallel right now
    pbs = False
    import socket
    if 'nasa' in socket.getfqdn():
        pbs = True

    cmd_list = []
    f_cmd = None

    if pbs:
        out_cmd_fn = o+'_cmd.sh'
        if not os.path.exists(out_cmd_fn):
            print("Creating text file of commands")
            f_cmd = open(out_cmd_fn, 'w')

    for n, tile in enumerate(out_tile_list):
        #print('%i of %i tiles: %i' % (n+1, len(out_tile_list), tile))
        tile_fn_base = '%s-tile-%0*i.tif' % (o, ni, tile)
        tile_fn_list_txt = os.path.splitext(tile_fn_base)[0]+'_fn_list.txt'
        #Write out DEM file list for the tile
        with open(tile_fn_list_txt, 'w') as f_fn_list:
            f_fn_list.write('\n'.join(tile_dict[tile]['fn_list']))
        for stat in stat_list:
            tile_fn = os.path.splitext(tile_fn_base)[0]+'-%s.tif' % stat
            dem_mos_threads = 1
            #Use more threads for tiles with many inputs, will take much longer to finish
            #Should do some analysis of totals for all fn_list
            if len(tile_dict[tile]['fn_list']) > 80:
                dem_mos_threads = 4
            dem_mosaic_args = {'fn_list':tile_dict[tile]['fn_list'], 'o':tile_fn, \
                    'fn_list_txt':tile_fn_list_txt, \
                    'tr':tr, 't_srs':t_srs, 't_projwin':tile_dict[tile]['extent'], \
                    'threads':dem_mos_threads, 'stat':stat}
            if not os.path.exists(tile_fn):
                cmd = geolib.get_dem_mosaic_cmd(**dem_mosaic_args)
                #Hack to clean up extra quotes around proj4 string here '""'
                cmd_list.append([s.replace('\"','') for s in cmd])
            if f_cmd is not None:
                #Write out command to file
                f_cmd.write('%s\n' % ' '.join(str(i) for i in cmd))

    f_cmd = None

    if pbs:
        stripecount = 28 
        iolib.setstripe(odir, stripecount)
        #Get number of available devel nodes, submit with 
        #$(node_stats.sh | grep -A 4 'devel' | grep Broadwell | awk '{print $NF}')
        pbs_script = os.path.join(os.path.split(os.path.realpath(__file__))[0], 'dem_mosaic_parallel.pbs')
        cmd = ['qsub', '-v', 'cmd_fn=%s' % out_cmd_fn, pbs_script]
        print(' '.join(str(i) for i in cmd))
        subprocess.call(cmd)
        #This is currently the hack to interrupt and wait for pbs to finish, then 'continue' in ipdb
        import ipdb; ipdb.set_trace()
        #print("qsub -v cmd_fn=%s %s" % (out_cmd_fn, pbs_script))
        #qtop_cmd = ['qtop_deshean.sh', '|', 'grep', 'dem_mos']
        #while qtop_cmd has output
        #Need to wait for job to finish, could get job id, then while qstat
    else:
        with ThreadPoolExecutor(max_workers=threads) as executor:
            print("Running dem_mosaic in parallel with %i threads" % threads)
            for cmd in cmd_list:
                executor.submit(subprocess.call, cmd, stdout=outf, stderr=subprocess.STDOUT)
            time.sleep(delay)

    #Now aggegate into stats
    #Could do this in parallel
    for stat in stat_list:
        tile_fn_list = []
        for n, tile in enumerate(out_tile_list):
            tile_fn_base = '%s-tile-%0*i.tif' % (o, ni, tile)
            tile_fn = os.path.splitext(tile_fn_base)[0]+'-%s.tif' % stat
            if os.path.exists(tile_fn):
                tile_fn_list.append(tile_fn)
        print("\nMosaic type: %s" % stat)
        #Convert dem_mosaic index files to timestamp arrays
        if stat in ['lastindex', 'firstindex', 'medianindex']:
            #Update filenames with ts.tif extension
            tile_fn_list_torun = [tile_fn for tile_fn in tile_fn_list if not os.path.exists(os.path.splitext(tile_fn)[0]+'_ts.tif')]
            if tile_fn_list_torun:
                print("Running dem_mosaic_index_ts in parallel with %i threads" % threads)
                from multiprocessing import Pool
                pool = Pool(processes=threads)
                results = pool.map(make_dem_mosaic_index_ts, tile_fn_list_torun)
                pool.close()
                #results.wait()
            tile_fn_list = [os.path.splitext(tile_fn)[0]+'_ts.tif' for tile_fn in tile_fn_list]

        print("\nCreating vrt of valid tiles")
        #tile_fn_list = glob.glob(o+'-tile-*.tif')
        vrt_fn = o+'.vrt'
        if stat is not None:
            vrt_fn = os.path.splitext(vrt_fn)[0]+'_%s.vrt' % stat
            if stat in ['lastindex', 'firstindex', 'medianindex']:
                vrt_fn = os.path.splitext(vrt_fn)[0]+'_ts.vrt'
        cmd = ['gdalbuildvrt'] 
        cmd.extend(['-r', 'cubic'])
        #cmd.append('-tap')
        cmd.append(vrt_fn)
        vrt_fn_list = []
        for tile_fn in tile_fn_list:
            if os.path.exists(tile_fn):
                vrt_fn_list.append(tile_fn)
            else:
                print("Missing file: %s" % tile_fn)
        cmd.extend(sorted(vrt_fn_list))
        print(cmd)
        subprocess.call(cmd)

        #Should create tile index shp/kml from tile_geom

        #This cleans up all of the log txt files (potentially 1000s of files)
        #Want to preserve these, as they contain list of DEMs that went into each tile
        log_fn_list = glob.glob(o+'*%s.tif-log-dem_mosaic-*.txt' % stat)
        print("\nCleaning up %i dem_mosaic log files" % len(log_fn_list))
        if stat is not None:
            tar_fn = o+'_%s_dem_mosaic_log.tar.gz' % stat
        else:
            tar_fn = o+'_dem_mosaic_log.tar.gz'
        with tarfile.open(tar_fn, "w:gz") as tar:
            for log_fn in log_fn_list:
                tar.add(log_fn)
        for log_fn in log_fn_list:
            os.remove(log_fn)

    outf = None

if __name__ == "__main__":
    main()
