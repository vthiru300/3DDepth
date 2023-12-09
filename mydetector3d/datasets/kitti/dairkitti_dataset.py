from scipy import linalg
from PIL import Image
import copy
import pickle
import torch
import numpy as np
from skimage import io
import json
from mydetector3d.datasets.kitti import kitti_utils
#from . import kitti_utils
from mydetector3d.ops.roiaware_pool3d import roiaware_pool3d_utils
from mydetector3d.utils import box_utils, calibration_kitti, common_utils, object3d_custom #object3d_kitti
from mydetector3d.datasets.dataset import DatasetTemplate


class DairKittiDataset(DatasetTemplate):
    def __init__(self, dataset_cfg, class_names, training=True, root_path=None, logger=None):
        """
        Args:
            root_path:
            dataset_cfg:
            class_names:
            training:
            logger:
        """
        super().__init__(
            dataset_cfg=dataset_cfg, class_names=class_names, training=training, root_path=root_path, logger=logger
        )
        self.split = self.dataset_cfg.DATA_SPLIT[self.mode]
        self.camera_config = self.dataset_cfg.get('CAMERA_CONFIG', None)
        if self.camera_config is not None:
            self.use_camera = self.camera_config.get('USE_CAMERA', True)
            self.camera_image_config = self.camera_config.IMAGE
        else:
            self.use_camera = False

        self.root_split_path = self.root_path / ('training' if self.split != 'test' else 'testing')

        split_dir = self.root_path / 'ImageSets' / (self.split + '.txt')
        self.sample_id_list = [x.strip() for x in open(split_dir).readlines()] if split_dir.exists() else None

        self.kitti_infos = [] #contain ground truth
        self.include_kitti_data(self.mode)

        self.map_class_to_kitti = self.dataset_cfg.MAP_CLASS_TO_KITTI #new added

        if self.dataset_cfg.Early_Fusion == True:
            f = open(self.dataset_cfg.I2Vmap_path, 'rb')   # if only use 'r' for reading; it will show error: 'utf-8' codec can't decode byte 0x80 in position 0: invalid start byte
            self.i2vmap = pickle.load(f)         # load file content as mydict 6601
            f.close()
            newkitti_infos= []
            for info in self.kitti_infos:
                #sample_idx = info['point_cloud']['lidar_idx']
                sample_idx_int = info['image']['image_idx'] #get sample idx
                sample_idx = '{:06d}'.format(int(sample_idx_int)) 
                v_lidarfile=sample_idx+'.bin'
                if v_lidarfile in self.i2vmap.keys():
                    i_binfilename=self.i2vmap[v_lidarfile]
                    i_lidarbin=Path(self.dataset_cfg.InfrastructureLidar_path) / i_binfilename
                    if i_lidarbin.exists():
                        newkitti_infos.append(info) #only select frames has infrastructure cooperation
            self.kitti_infos = newkitti_infos #12228<-5250
            

    def include_kitti_data(self, mode):
        if self.logger is not None:
            self.logger.info('Loading DAIRKITTI dataset')
        kitti_infos = []

        for info_path in self.dataset_cfg.INFO_PATH[mode]: #INFO_PATH in dataset yaml file (infos_train.pkl)
            info_path = self.root_path / info_path
            if not info_path.exists():
                continue
            with open(info_path, 'rb') as f:
                infos = pickle.load(f)
                kitti_infos.extend(infos)

        self.kitti_infos.extend(kitti_infos)

        if self.logger is not None:
            self.logger.info('Total samples for DAIR KITTI dataset: %d' % (len(kitti_infos)))

    def set_split(self, split):
        super().__init__(
            dataset_cfg=self.dataset_cfg, class_names=self.class_names, training=self.training, root_path=self.root_path, logger=self.logger
        )
        self.split = split
        self.root_split_path = self.root_path / ('training' if self.split != 'test' else 'testing')

        split_dir = self.root_path / 'ImageSets' / (self.split + '.txt')
        self.sample_id_list = [x.strip() for x in open(split_dir).readlines()] if split_dir.exists() else None

    
    def get_fusion(self, path_c_data_info):
        #c_data_info = read_json(path_c_data_info)
        with open(path_c_data_info, "r") as load_f:
            c_data_info = json.load(load_f)
        #info = copy.deepcopy(self.kitti_infos[index]) #get info dict from index-th frame in kitti_infos array
        for info in self.kitti_infos:
            #sample_idx = info['point_cloud']['lidar_idx']
            sample_idx_int = info['image']['image_idx'] #get sample idx
            sample_idx = '{:06d}'.format(int(sample_idx_int)) 
            img_shape = info['image']['image_shape'] #get image width and height

    def get_lidar(self, idx):
        v_binfilename = '%s.bin' % idx
        lidar_file = self.root_split_path / 'velodyne' / v_binfilename
        assert lidar_file.exists()
        v_points = np.fromfile(str(lidar_file), dtype=np.float32).reshape(-1, 4)
        if self.dataset_cfg.Early_Fusion == True and self.dataset_cfg.Lidar_Fusion:
            if v_binfilename in self.i2vmap.keys():
                i_binfilename=self.i2vmap[v_binfilename]
                i_lidarbin=Path(self.dataset_cfg.InfrastructureLidar_path) / i_binfilename
                i_points = np.fromfile(i_lidarbin, dtype=np.float32).reshape(-1, 4)
                points = np.append(v_points, i_points, axis=0)
                mask = ~np.isnan(points[:, 0]) & ~np.isnan(points[:, 1]) & ~np.isnan(points[:, 2]) & ~np.isnan(points[:, 3])
                #print("removed nan points: ", sum(~mask))
                v_points=points[mask,:]
            else:
                print("Binfile key not available:", v_binfilename)
        return v_points


    def get_image(self, idx):
        """
        Loads image for a sample
        Args:
            idx: int, Sample index
        Returns:
            image: (H, W, 3), RGB Image
        """
        img_file = self.root_split_path / 'image_2' / ('%s.jpg' % idx) #Kitti is 'image_2' .png
        assert img_file.exists()
        image = io.imread(img_file)
        image = image.astype(np.float32)
        image /= 255.0
        return image
    
    
    def getandcrop_image(self, idx, input_dict):
        from PIL import Image
        img_file = self.root_split_path / 'image_2' / ('%s.jpg' % idx) #Kitti is 'image_2' .png
        assert img_file.exists()
        images = []
        images.append(Image.open(str(img_file)))
        input_dict["ori_shape"] = images[0].size
        input_dict["camera_imgs"] = images

        W, H = input_dict["ori_shape"]
        imgs = input_dict["camera_imgs"]
        img_process_infos = []
        crop_images = []
        for img in imgs:
            if self.training == True:
                fH, fW = self.camera_image_config.FINAL_DIM
                resize_lim = self.camera_image_config.RESIZE_LIM_TRAIN
                resize = np.random.uniform(*resize_lim)
                resize_dims = (int(W * resize), int(H * resize))
                newW, newH = resize_dims
                crop_h = newH - fH
                crop_w = int(np.random.uniform(0, max(0, newW - fW)))
                crop = (crop_w, crop_h, crop_w + fW, crop_h + fH)
            else:
                fH, fW = self.camera_image_config.FINAL_DIM
                resize_lim = self.camera_image_config.RESIZE_LIM_TEST
                resize = np.mean(resize_lim)
                resize_dims = (int(W * resize), int(H * resize))
                newW, newH = resize_dims
                crop_h = newH - fH
                crop_w = int(max(0, newW - fW) / 2)
                crop = (crop_w, crop_h, crop_w + fW, crop_h + fH)
            
            # reisze and crop image
            img = img.resize(resize_dims)
            img = img.crop(crop)
            crop_images.append(img)
            img_process_infos.append([resize, crop, False, 0])
        
        input_dict['img_process_infos'] = img_process_infos
        input_dict['camera_imgs'] = crop_images
        return input_dict

    def get_image_shape(self, idx):
        img_file = self.root_split_path / 'image_2' / ('%s.jpg' % idx)
        assert img_file.exists()
        return np.array(io.imread(img_file).shape[:2], dtype=np.int32)

    def get_label(self, idx):
        label_file = self.root_split_path / 'label_2' / ('%s.txt' % idx) #Kitti is 'label_2'
        assert label_file.exists()

        #read everyline as object3d class
        #return objects[] list, contain object information,e.g., type, xy
        #return object3d_kitti.get_objects_from_label(label_file)
        return object3d_custom.get_objects_from_label(label_file)

    def get_depth_map(self, idx): #not used
        """
        Loads depth map for a sample
        Args:
            idx: str, Sample index
        Returns:
            depth: (H, W), Depth map
        """
        depth_file = self.root_split_path / 'depth_2' / ('%s.png' % idx)
        assert depth_file.exists()
        depth = io.imread(depth_file)
        depth = depth.astype(np.float32)
        depth /= 256.0
        return depth

    def get_calib(self, idx):
        calib_file = self.root_split_path / 'calib' / ('%s.txt' % idx)
        #print("calib_file:", calib_file)
        assert calib_file.exists()
        return calibration_kitti.Calibration(calib_file)

    def get_road_plane(self, idx):
        plane_file = self.root_split_path / 'planes' / ('%s.txt' % idx)
        if not plane_file.exists():
            return None

        with open(plane_file, 'r') as f:
            lines = f.readlines()
        lines = [float(i) for i in lines[3].split()]
        plane = np.asarray(lines)

        # Ensure normal is always facing up, this is in the rectified camera coordinate
        if plane[1] > 0:
            plane = -plane

        norm = np.linalg.norm(plane[0:3])
        plane = plane / norm
        return plane

    @staticmethod
    def get_fov_flag(pts_rect, img_shape, calib):
        """
        Args:
            pts_rect: lidar points in rect coordinate
            img_shape:
            calib:

        Returns:

        """
        pts_img, pts_rect_depth = calib.rect_to_img(pts_rect)
        #check whether the projected points in the image range
        val_flag_1 = np.logical_and(pts_img[:, 0] >= 0, pts_img[:, 0] < img_shape[1])
        val_flag_2 = np.logical_and(pts_img[:, 1] >= 0, pts_img[:, 1] < img_shape[0])
        val_flag_merge = np.logical_and(val_flag_1, val_flag_2)
        #depth should also >0
        pts_valid_flag = np.logical_and(val_flag_merge, pts_rect_depth >= 0) #[True, False] list

        return pts_valid_flag

    def get_infos(self, num_workers=4, has_label=True, count_inside_pts=True, sample_id_list=None):
        import concurrent.futures as futures

        def process_single_scene(sample_idx): #for each idx in the list
            print('%s sample_idx: %s' % (self.split, sample_idx))
            info = {}
            #point cloud info
            pc_info = {'num_features': 4, 'lidar_idx': sample_idx}
            info['point_cloud'] = pc_info

            image_info = {'image_idx': sample_idx, 'image_shape': self.get_image_shape(sample_idx)}
            info['image'] = image_info
            calib = self.get_calib(sample_idx)

            P2 = np.concatenate([calib.P2, np.array([[0., 0., 0., 1.]])], axis=0)
            R0_4x4 = np.zeros([4, 4], dtype=calib.R0.dtype)
            R0_4x4[3, 3] = 1.
            R0_4x4[:3, :3] = calib.R0
            V2C_4x4 = np.concatenate([calib.V2C, np.array([[0., 0., 0., 1.]])], axis=0)
            calib_info = {'P2': P2, 'R0_rect': R0_4x4, 'Tr_velo_to_cam': V2C_4x4}

            info['calib'] = calib_info

            if has_label:
                obj_list = self.get_label(sample_idx)
                num_obj = len(obj_list)
                annotations = {}
                if num_obj<=0:
                    print("Object list is empty")
                    annotations['bbox'] = np.array([])
                    annotations['location'] = np.array([])
                else:
                    annotations['bbox'] = np.concatenate([obj.box2d.reshape(1, 4) for obj in obj_list], axis=0)
                    annotations['location'] = np.concatenate([obj.loc.reshape(1, 3) for obj in obj_list], axis=0)

                annotations['name'] = np.array([obj.cls_type for obj in obj_list])
                annotations['truncated'] = np.array([obj.truncation for obj in obj_list])
                annotations['occluded'] = np.array([obj.occlusion for obj in obj_list])
                annotations['alpha'] = np.array([obj.alpha for obj in obj_list])
                annotations['dimensions'] = np.array([[obj.l, obj.h, obj.w] for obj in obj_list])  # lhw(camera) format
                annotations['rotation_y'] = np.array([obj.ry for obj in obj_list])
                annotations['score'] = np.array([obj.score for obj in obj_list])
                annotations['difficulty'] = np.array([obj.level for obj in obj_list], np.int32)

                num_objects = len([obj.cls_type for obj in obj_list if obj.cls_type != 'DontCare']) #effective objects (excluding DontCare)
                num_gt = len(annotations['name']) #total objects
                if num_objects>0:
                    index = list(range(num_objects)) + [-1] * (num_gt - num_objects)
                    annotations['index'] = np.array(index, dtype=np.int32)#e.g., index=[0,1,2,3,4,5,-1,-1,-1,-1]

                    #N is effective objects location（N,3）、dimensions（N,3）、rotation_y（N,1）
                    loc = annotations['location'][:num_objects] #get 0:num_objects, DontCare object is always at the end
                    dims = annotations['dimensions'][:num_objects]
                    rots = annotations['rotation_y'][:num_objects]

                    #Kitti 3D annotation is in camera coordinate, convert it to Lidar coordinate
                    loc_lidar = calib.rect_to_lidar(loc)
                    #dimension 0,1,2 column is l,h,w
                    l, h, w = dims[:, 0:1], dims[:, 1:2], dims[:, 2:3]

                    #shift objects' center coordinate (original 0) from box bottom to the center
                    loc_lidar[:, 2] += h[:, 0] / 2

                    # (N, 7) [x, y, z, dx, dy, dz, heading]
                    # np.newaxis add one dimension in column，rots is (N,)
                    # -(np.pi / 2 + rots[..., np.newaxis]): convert kitti camera rot angle definition to pcdet lidar rot angle definition.
                    #  In kitti，camera坐标系下定义物体朝向与camera的x轴夹角顺时针为正，逆时针为负
                    # 在pcdet中，lidar坐标系下定义物体朝向与lidar的x轴夹角逆时针为正，顺时针为负，所以二者本身就正负相反
                    # pi / 2是坐标系x轴相差的角度(如图所示)
                    # camera:         lidar:
                    # Y                    X
                    # |                    |
                    # |____X         Y_____|     
                    gt_boxes_lidar = np.concatenate([loc_lidar, l, w, h, -(np.pi / 2 + rots[..., np.newaxis])], axis=1)
                    annotations['gt_boxes_lidar'] = gt_boxes_lidar
                else:
                    gt_boxes_lidar = np.zeros((0, 9))
                    annotations['gt_boxes_lidar'] = gt_boxes_lidar

                info['annos'] = annotations

                if count_inside_pts and num_gt>0:
                    points = self.get_lidar(sample_idx) #get lidar points based on index list
                    calib = self.get_calib(sample_idx)
                    pts_rect = calib.lidar_to_rect(points[:, 0:3]) #convert points from lidar coordinate to camera rect coordinate

                    fov_flag = self.get_fov_flag(pts_rect, info['image']['image_shape'], calib) #True/False list of points inside the camera fov
                    pts_fov = points[fov_flag] #only select points inside the camera FOV

                    # gt_boxes_lidar is (N,7)  [x, y, z, dx, dy, dz, heading], (x, y, z) is the box center
                    # returned corners_lidar（N,8,3）:8 point box for each box (each point is the coordinate)
                    corners_lidar = box_utils.boxes_to_corners_3d(gt_boxes_lidar)

                    # num_gt is the total number object in the current frame，
                    # initialize num_points_in_gt=array([-1, -1, -1, -1, -1, -1, -1, -1, -1, -1], dtype=int32)
                    num_points_in_gt = -np.ones(num_gt, dtype=np.int32)
                    #num_objects is effective object numbers
                    for k in range(num_objects):
                        #corners_lidar the 8 point box of the k-th gt, pts_fov is the lidar points inside the camera FOV
                        #is_hull check whether the point cloud is inside the bbox or not, use 0:3 means only check 2D box (x,y)
                        flag = box_utils.in_hull(pts_fov[:, 0:3], corners_lidar[k])
                        num_points_in_gt[k] = flag.sum() #calculate the points inside the box
                    annotations['num_points_in_gt'] = num_points_in_gt
                elif count_inside_pts and num_gt==0:
                    annotations['num_points_in_gt'] =  np.array([]) #np.zeros()

            return info

        sample_id_list = sample_id_list if sample_id_list is not None else self.sample_id_list
        with futures.ThreadPoolExecutor(num_workers) as executor:
            infos = executor.map(process_single_scene, sample_id_list) #process train or val sample id list
        return list(infos)

    #use groundtruth in trainfile to generate groundtruth_database folder
    def create_groundtruth_database(self, info_path=None, used_classes=None, split='train'):
        #create gt_database folder
        database_save_path = Path(self.root_path) / ('gt_database' if split == 'train' else ('gt_database_%s' % split))
        #save kitti_dbinfos_train file under kitti folder
        db_info_save_path = Path(self.root_path) / ('kitti_dbinfos_%s.pkl' % split)

        database_save_path.mkdir(parents=True, exist_ok=True)
        all_db_infos = {}

        with open(info_path, 'rb') as f:
            infos = pickle.load(f) #load pkl file: kitti_infos_train.pkl

        for k in range(len(infos)): #read every info (each frame) in infos array
            print('gt_database sample: %d/%d' % (k + 1, len(infos)))
            info = infos[k]
            sample_idx = info['point_cloud']['lidar_idx']#get index list in train.txt
            #Read lidar points [M,4]
            points = self.get_lidar(sample_idx)
            annos = info['annos'] #read annotation
            names = annos['name'] #
            difficulty = annos['difficulty']
            bbox = annos['bbox']
            gt_boxes = annos['gt_boxes_lidar']

            num_obj = gt_boxes.shape[0]
            if num_obj >0:
                point_indices = roiaware_pool3d_utils.points_in_boxes_cpu(
                    torch.from_numpy(points[:, 0:3]), torch.from_numpy(gt_boxes)
                ).numpy()  # (nboxes, npoints)

            for i in range(num_obj):
                filename = '%s_%s_%d.bin' % (sample_idx, names[i], i)
                filepath = database_save_path / filename
                gt_points = points[point_indices[i] > 0]

                gt_points[:, :3] -= gt_boxes[i, :3]
                with open(filepath, 'w') as f:
                    gt_points.tofile(f)

                if (used_classes is None) or names[i] in used_classes:
                    db_path = str(filepath.relative_to(self.root_path))  # gt_database/xxxxx.bin
                    db_info = {'name': names[i], 'path': db_path, 'image_idx': sample_idx, 'gt_idx': i,
                               'box3d_lidar': gt_boxes[i], 'num_points_in_gt': gt_points.shape[0],
                               'difficulty': difficulty[i], 'bbox': bbox[i], 'score': annos['score'][i]}
                    if names[i] in all_db_infos:
                        all_db_infos[names[i]].append(db_info)
                    else:
                        all_db_infos[names[i]] = [db_info]
        for k, v in all_db_infos.items():
            print('Database %s: %d' % (k, len(v)))

        with open(db_info_save_path, 'wb') as f:
            pickle.dump(all_db_infos, f)

    @staticmethod
    def generate_prediction_dicts(batch_dict, pred_dicts, class_names, output_path=None):
        """
        Args:
            batch_dict:
                frame_id:
            pred_dicts: list of pred_dicts
                pred_boxes: (N, 7), Tensor
                pred_scores: (N), Tensor
                pred_labels: (N), Tensor
            class_names:
            output_path:

        Returns:

        """
        def get_template_prediction(num_samples):
            ret_dict = {
                'name': np.zeros(num_samples), 'truncated': np.zeros(num_samples),
                'occluded': np.zeros(num_samples), 'alpha': np.zeros(num_samples),
                'bbox': np.zeros([num_samples, 4]), 'dimensions': np.zeros([num_samples, 3]),
                'location': np.zeros([num_samples, 3]), 'rotation_y': np.zeros(num_samples),
                'score': np.zeros(num_samples), 'boxes_lidar': np.zeros([num_samples, 7])
            }
            return ret_dict

        def generate_single_sample_dict(batch_index, box_dict):
            pred_scores = box_dict['pred_scores'].cpu().numpy()
            pred_boxes = box_dict['pred_boxes'].cpu().numpy()
            pred_labels = box_dict['pred_labels'].cpu().numpy()
            pred_dict = get_template_prediction(pred_scores.shape[0])
            if pred_scores.shape[0] == 0:
                return pred_dict

            calib = batch_dict['calib'][batch_index]
            image_shape = batch_dict['image_shape'][batch_index].cpu().numpy()
            pred_boxes_camera = box_utils.boxes3d_lidar_to_kitti_camera(pred_boxes, calib)
            pred_boxes_img = box_utils.boxes3d_kitti_camera_to_imageboxes(
                pred_boxes_camera, calib, image_shape=image_shape
            )

            pred_dict['name'] = np.array(class_names)[pred_labels - 1]
            pred_dict['alpha'] = -np.arctan2(-pred_boxes[:, 1], pred_boxes[:, 0]) + pred_boxes_camera[:, 6]
            pred_dict['bbox'] = pred_boxes_img
            pred_dict['dimensions'] = pred_boxes_camera[:, 3:6]
            pred_dict['location'] = pred_boxes_camera[:, 0:3]
            pred_dict['rotation_y'] = pred_boxes_camera[:, 6]
            pred_dict['score'] = pred_scores
            pred_dict['boxes_lidar'] = pred_boxes

            return pred_dict

        annos = []
        for index, box_dict in enumerate(pred_dicts):
            frame_id = batch_dict['frame_id'][index]

            single_pred_dict = generate_single_sample_dict(index, box_dict)
            single_pred_dict['frame_id'] = frame_id
            annos.append(single_pred_dict)

            if output_path is not None:
                cur_det_file = output_path / ('%s.txt' % frame_id)
                with open(cur_det_file, 'w') as f:
                    bbox = single_pred_dict['bbox']
                    loc = single_pred_dict['location']
                    dims = single_pred_dict['dimensions']  # lhw -> hwl

                    for idx in range(len(bbox)):
                        print('%s -1 -1 %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f'
                              % (single_pred_dict['name'][idx], single_pred_dict['alpha'][idx],
                                 bbox[idx][0], bbox[idx][1], bbox[idx][2], bbox[idx][3],
                                 dims[idx][1], dims[idx][2], dims[idx][0], loc[idx][0],
                                 loc[idx][1], loc[idx][2], single_pred_dict['rotation_y'][idx],
                                 single_pred_dict['score'][idx]), file=f)

        return annos

    def evaluation(self, det_annos, class_names, **kwargs):
        if 'annos' not in self.kitti_infos[0].keys():
            return None, {}

        from .kitti_object_eval_python import eval as kitti_eval

        eval_det_annos = copy.deepcopy(det_annos)
        eval_gt_annos = [copy.deepcopy(info['annos']) for info in self.kitti_infos]
        
        ap_result_str, ap_dict = kitti_eval.get_official_eval_result(eval_gt_annos, eval_det_annos, class_names)

        return ap_result_str, ap_dict

    def __len__(self):
        if self._merge_all_iters_to_one_epoch:
            return len(self.kitti_infos) * self.total_epochs

        return len(self.kitti_infos)

    def load_camera_info(self, input_dict, calib):
        from pyquaternion import Quaternion
        input_dict["image_paths"] = []
        input_dict["lidar2camera"] = []
        input_dict["lidar2image"] = []
        input_dict["camera2ego"] = []
        input_dict["camera_intrinsics"] = []
        input_dict["camera2lidar"] = []

        V2R_44, P2_34 = kitti_utils.calib_to_matricies(calib)
        #return (4,4) (3,4)
        C2V_44, R0_33, intrinsics_cam2_33 = kitti_utils.calib_to_intrinsics(calib)
        input_dict["lidar2camera"].append(V2R_44)
        camera_intrinsics = np.eye(4).astype(np.float32)
        camera_intrinsics[:3, :3] = intrinsics_cam2_33
        input_dict["camera_intrinsics"].append(camera_intrinsics)

        # lidar to image transform
        lidar2image = camera_intrinsics @ V2R_44.T #lidar2camera
        input_dict["lidar2image"].append(lidar2image)

         # camera to ego transform
        camera2ego = np.eye(4).astype(np.float32)
        input_dict["camera2ego"].append(camera2ego)

        # camera to lidar transform
        camera2lidar = np.eye(4).astype(np.float32)
        input_dict["camera2lidar"].append(camera2lidar)

        return input_dict

    def set_lidar_aug_matrix(self, data_dict):
        """
            Get lidar augment matrix (4 x 4), which are used to recover orig point coordinates.
        """
        lidar_aug_matrix = np.eye(4)
        if 'flip_y' in data_dict.keys():
            flip_x = data_dict['flip_x']
            flip_y = data_dict['flip_y']
            if flip_x:
                lidar_aug_matrix[:3,:3] = np.array([[1, 0, 0], [0, -1, 0], [0, 0, 1]]) @ lidar_aug_matrix[:3,:3]
            if flip_y:
                lidar_aug_matrix[:3,:3] = np.array([[-1, 0, 0], [0, 1, 0], [0, 0, 1]]) @ lidar_aug_matrix[:3,:3]
        if 'noise_rot' in data_dict.keys():
            noise_rot = data_dict['noise_rot']
            lidar_aug_matrix[:3,:3] = common_utils.angle2matrix(torch.tensor(noise_rot)) @ lidar_aug_matrix[:3,:3]
        if 'noise_scale' in data_dict.keys():
            noise_scale = data_dict['noise_scale']
            lidar_aug_matrix[:3,:3] *= noise_scale
        if 'noise_translate' in data_dict.keys():
            noise_translate = data_dict['noise_translate']
            lidar_aug_matrix[:3,3:4] = noise_translate.T
        data_dict['lidar_aug_matrix'] = lidar_aug_matrix
        return data_dict
    
    def __getitem__(self, index):
        #print("get item: ", index)
        # index = 4
        if self._merge_all_iters_to_one_epoch:
            index = index % len(self.kitti_infos)

        info = copy.deepcopy(self.kitti_infos[index]) #get info dict from index-th frame in kitti_infos array

        #sample_idx = info['point_cloud']['lidar_idx']
        sample_idx_int = info['image']['image_idx'] #get sample idx
        sample_idx = '{:06d}'.format(int(sample_idx_int)) 
        img_shape = info['image']['image_shape'] #get image width and height
        calib = self.get_calib(sample_idx)#get calibration object (P2, R0, V2C)
        get_item_list = self.dataset_cfg.get('GET_ITEM_LIST', ['points']) #item list

        #define input_dict with sample idx and calib
        input_dict = {
            'frame_id': sample_idx,
            'calib': calib,
        }

        if 'annos' in info:
            annos = info['annos'] #get annotation
            annos = common_utils.drop_info_with_name(annos, name='DontCare') #remove 'DontCare'
            #get location, dimension, and rotation angle
            loc, dims, rots = annos['location'], annos['dimensions'], annos['rotation_y']
            gt_names = annos['name']

            if len(gt_names)==0:
                gt_boxes_lidar = np.zeros((0, 7))
            else:
                #Kitti 3D annotation is in camera coordinate
                #create label [n,7] in camera coordinate boxes3d_camera: (N, 7) [x, y, z, l, h, w, r] in rect camera coords
                gt_boxes_camera = np.concatenate([loc, dims, rots[..., np.newaxis]], axis=1).astype(np.float32)
                #convert camera coordinate to Lidar coordinate  boxes3d_lidar: [x, y, z, dx, dy, dz, heading], (x, y, z) is the box center
                gt_boxes_lidar = box_utils.boxes3d_kitti_camera_to_lidar(gt_boxes_camera, calib)

            #add new data to input_dict
            input_dict.update({
                'gt_names': gt_names,
                'gt_boxes': gt_boxes_lidar
            })
            if "gt_boxes2d" in get_item_list:
                input_dict['gt_boxes2d'] = annos["bbox"] #add 2D box from annotation

            road_plane = self.get_road_plane(sample_idx)
            if road_plane is not None:
                input_dict['road_plane'] = road_plane

        if "points" in get_item_list: #add Lidar points to input_dict
            points = self.get_lidar(sample_idx) #get lidar points
            if self.dataset_cfg.FOV_POINTS_ONLY: #require FOV angle, cut the Lidar points to camera view only
                pts_rect = calib.lidar_to_rect(points[:, 0:3])
                fov_flag = self.get_fov_flag(pts_rect, img_shape, calib)
                points = points[fov_flag]
            input_dict['points'] = points

        if "images" in get_item_list:
            input_dict['images'] = self.get_image(sample_idx)
            input_dict = self.getandcrop_image(sample_idx, input_dict)

        if "depth_maps" in get_item_list:
            input_dict['depth_maps'] = self.get_depth_map(sample_idx)

        if "calib_matricies" in get_item_list:
            input_dict["trans_lidar_to_cam"], input_dict["trans_cam_to_img"] = kitti_utils.calib_to_matricies(calib)
            input_dict=self.load_camera_info(input_dict, calib)

        input_dict = self.set_lidar_aug_matrix(input_dict)
        data_dict = self.prepare_data(data_dict=input_dict) #send input_dict to prepare_data to generate training data

        data_dict['image_shape'] = img_shape
        return data_dict


def create_kitti_infos(dataset_cfg, class_names, data_path, save_path, workers=4):
    dataset = DairKittiDataset(dataset_cfg=dataset_cfg, class_names=class_names, root_path=data_path, training=False)
    train_split, val_split = 'train', 'val'

    train_filename = save_path / ('kitti_infos_%s.pkl' % train_split)
    val_filename = save_path / ('kitti_infos_%s.pkl' % val_split)
    trainval_filename = save_path / 'kitti_infos_trainval.pkl'
    test_filename = save_path / 'kitti_infos_test.pkl'

    print('---------------Start to generate data infos---------------')

    dataset.set_split(train_split)
    kitti_infos_train = dataset.get_infos(num_workers=workers, has_label=True, count_inside_pts=True)
    with open(train_filename, 'wb') as f:
        pickle.dump(kitti_infos_train, f)
    print('Kitti info train file is saved to %s' % train_filename)

    dataset.set_split(val_split)
    kitti_infos_val = dataset.get_infos(num_workers=workers, has_label=True, count_inside_pts=True)
    with open(val_filename, 'wb') as f:
        pickle.dump(kitti_infos_val, f)
    print('Kitti info val file is saved to %s' % val_filename)

    with open(trainval_filename, 'wb') as f:
        pickle.dump(kitti_infos_train + kitti_infos_val, f)
    print('Kitti info trainval file is saved to %s' % trainval_filename)

    dataset.set_split('test')
    kitti_infos_test = dataset.get_infos(num_workers=workers, has_label=False, count_inside_pts=False)
    with open(test_filename, 'wb') as f:
        pickle.dump(kitti_infos_test, f)
    print('Kitti info test file is saved to %s' % test_filename)

    print('---------------Start create groundtruth database for data augmentation---------------')
    dataset.set_split(train_split)
    dataset.create_groundtruth_database(train_filename, split=train_split)

    print('---------------Data preparation Done---------------')


def checklabelfiles(root_path, folder):
    path_list = [path for path in glob(os.path.join(root_path, folder, "*.txt"))]
    print(len(path_list))#12424
    classname_count={}
    for label_file in path_list:
        #object3d_custom.get_objects_from_label(label_file)
        with open(label_file, 'r') as f:
            lines = f.readlines()
        for line in lines:
            label = line.strip().split(' ')
            cls_type = label[0]
            if cls_type in classname_count.keys():
                classname_count[cls_type]=classname_count[cls_type]+1
            else:
                classname_count[cls_type]=1
    return classname_count

def replacelabelfiles(root_path, folder, find_strs, replace_str):
    path_list = [path for path in glob(os.path.join(root_path, folder, "*.txt"))]
    print(len(path_list))#12424
    # find_strs = ["Truck","Van","Bus","Car"]
    # replace_str = "Car"
    for label_file in path_list:
        kitti_utils.replaceclass_txt(label_file, find_strs, replace_str)

def checkinfopklfiles(pklfile_folder, train_split='train'):
    kitti_infos=[]
    train_filename = pklfile_folder / ('kitti_infos_%s.pkl' % train_split)
    object_nums={}
    with open(train_filename, 'rb') as f:
        infos = pickle.load(f)
        kitti_infos.extend(infos)
    for info in kitti_infos:
        for key in info.keys():
            print(key)
        annotations=info['annos']
        lidar_idx=info['point_cloud']['lidar_idx']
        object_nums[lidar_idx]=len(annotations['name'])
    return object_nums
        # for key in annotations.keys():
        #     print("annotations:", key)
        #     print(annotations[key].shape)

from pathlib import Path
import os
if __name__ == '__main__':
    import argparse
    import yaml
    from easydict import EasyDict
    from glob import glob

    parser = argparse.ArgumentParser(description='arg parser')
    parser.add_argument('--cfg_file', type=str, default='mydetector3d/tools/cfgs/dataset_configs/dairkitti_dataset.yaml', help='specify the config of dataset')
    parser.add_argument('--func', type=str, default='testdataset', help='')
    parser.add_argument('--inputfolder', type=str, default='/data/cmpe249-fa22/DAIR-C/single-vehicle-side-point-cloud-kitti/', help='')
    parser.add_argument('--outputfolder', type=str, default='/data/cmpe249-fa22/DAIR-C/single-vehicle-side-point-cloud-kitti/', help='')

    args = parser.parse_args()

    ROOT_DIR = (Path(__file__).resolve().parent / '../../../').resolve()
    try:
        yaml_config = yaml.safe_load(open(args.cfg_file), Loader=yaml.FullLoader)
    except:
        yaml_config = yaml.safe_load(open(args.cfg_file))
    dataset_cfg = EasyDict(yaml_config)

    trainingfolder=os.path.join(args.inputfolder,'training')
    if args.func == 'create_split':
        
        kitti_utils.create_trainvaltestsplitfile(trainingfolder, args.outputfolder)
    elif args.func == 'create_infos':
        create_kitti_infos(
            dataset_cfg=dataset_cfg,
            class_names=['Car', 'Pedestrian', 'Cyclist', 'Other'],
            data_path=Path(args.inputfolder),
            save_path=Path(args.outputfolder)
        )
    elif args.func == 'checklabelfiles':
        classname_count = checklabelfiles(trainingfolder, 'label_2')
        print(classname_count)
    elif args.func == 'replacelabelnames':
        classname_count = checklabelfiles(trainingfolder, 'label_2')
        print(classname_count)
        find_strs = ["Motorcyclist", "Tricyclist"] # ["Truck","Van","Bus","Car"]
        replace_str = "Cyclist" #"Car"
        replacelabelfiles(trainingfolder, 'label_2', find_strs, replace_str)
        classname_count = checklabelfiles(trainingfolder, 'label_2')
        print(classname_count)
        find_strs = ["Trafficcone", "Barrowlist"] # ["Truck","Van","Bus","Car"]
        replace_str = "Other" #"Car"
        replacelabelfiles(trainingfolder, 'label_2', find_strs, replace_str)
        classname_count = checklabelfiles(trainingfolder, 'label_2')
        print(classname_count)
    elif args.func == 'checkinfopklfiles':
        object_nums=checkinfopklfiles(Path(args.inputfolder), train_split='train')
        print(object_nums)
    elif args.func == 'testdataset':
        lidarpath_list = [path for path in glob(os.path.join(args.inputfolder, 'training', 'velodyne', "*.bin"))]
        print("total lidar files:", len(lidarpath_list))
        from torch.utils.data import DataLoader
        dataset = DairKittiDataset(dataset_cfg=dataset_cfg, class_names=['Car', 'Pedestrian', 'Cyclist', 'Other'], root_path=Path(args.inputfolder), training=True)
        print("Dataset infos len:", len(dataset.kitti_infos)) #123580
        print("One info keys:")
        for key in dataset.kitti_infos[0]:
            print(key)
        for key in dataset.kitti_infos[0]['annos']:
            print(key)
        dataloader = DataLoader(
        dataset, batch_size=4, pin_memory=True, num_workers=1,
        shuffle=None, collate_fn=dataset.collate_batch,
        drop_last=False, sampler=None, timeout=0, worker_init_fn=None)
        print("dataloader len:", len(dataloader))
        iterator=iter(dataloader)
        onebatch=next(iterator)
        print(onebatch)


# if __name__ == '__main__':
#     import sys
#     if sys.argv.__len__() > 1 and sys.argv[1] == 'create_kitti_infos':
#         import yaml
#         from pathlib import Path
#         from easydict import EasyDict
#         dataset_cfg = EasyDict(yaml.load(open(sys.argv[2])))
#         ROOT_DIR = (Path(__file__).resolve().parent / '../../../').resolve()
#         create_kitti_infos(
#             dataset_cfg=dataset_cfg,
#             class_names=['Car', 'Pedestrian', 'Cyclist'],
#             data_path=ROOT_DIR / 'data' / 'kitti',
#             save_path=ROOT_DIR / 'data' / 'kitti'
#         )