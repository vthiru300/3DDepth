import numpy as np
import struct
import sys
import os
import open3d as o3d
#ref: https://github.com/cuge1995/bin-to-pcd-python/blob/master/bin2pcd.py

def bin_to_pcd(binFileName):
    size_float = 4
    list_pcd = []
    with open(binFileName, "rb") as f:
        byte = f.read(size_float * 4)
        while byte:
            x, y, z, intensity = struct.unpack("ffff", byte)
            list_pcd.append([x, y, z])
            byte = f.read(size_float * 4)
    np_pcd = np.asarray(list_pcd)
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(np_pcd)
    return pcd

def convertonefile(binFileName, pcdFileName):
    pcd = bin_to_pcd(binFileName)
    o3d.io.write_point_cloud(pcdFileName, pcd)

def convertfolder(binFolderName, pcdFolderName):
    for i in os.listdir(binFolderName):
        binFileName = binFolderName+i
        print(i)

        pcd = bin_to_pcd(binFileName)
        pcdFileName = pcdFolderName+i[:-4]+'.pcd'
        print(pcdFileName)
        o3d.io.write_point_cloud(pcdFileName, pcd)

if __name__ == "__main__":
    a = sys.argv[1]
    b = sys.argv[2]
    convertonefile(a, b)